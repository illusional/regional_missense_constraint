import logging
from typing import Dict, List, Tuple, Union

import hail as hl

from gnomad.utils.reference_genome import get_reference_genome
from gnomad_lof.constraint_utils.generic import annotate_variant_types
from rmc.utils.generic import (
    filter_to_missense,
    get_coverage_correction_expr,
    get_exome_bases,
    get_plateau_model,
    keep_criteria,
)


logging.basicConfig(
    format="%(asctime)s (%(name)s %(lineno)s): %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger("constraint_utils")
logger.setLevel(logging.INFO)


def calculate_observed(ht: hl.Table, exac: bool) -> hl.Table:
    """
    Groups input Table by transcript, filters based on `keep_criteria`,
    and aggregates observed variants count per transcript.

    :param hl.Table ht: Input Table.
    :param bool exac: Whether the input Table is ExAC data.
    :return: Table annotated with observed variant counts.
    :rtype: hl.Table
    """
    ht = ht.filter(keep_criteria(ht, exac))
    return ht.group_by(ht.transcript).aggregate(observed=hl.agg.count())


def calculate_exp_per_base(
    context_ht: hl.Table,
    groupings: List[str] = [
        "context",
        "ref",
        "alt",
        "cpg",
        "methylation_level",
        "exome_coverage",
    ],
) -> hl.Table:
    """
    Returns table with expected variant counts annotated per base. 

    Expected variants count is mutation rate per SNP adjusted by CpG status and coverage.

    .. note::
        Expects:
        - context_ht is annotated with mutation rate (`mu_snp`).
        - context_ht contains coverage and plateau models in global annotations (`coverage_model`, `plateau_models`).

    :param hl.Table context_ht: Context Table.
    :param List[str] groupings: List of Table fields used to group Table to adjust mutation rate. 
        Table must be annotated with these fields. Default fields are context, ref, alt, cpg, and exome_coverage.
    :return: Table grouped by transcript with expected variant counts per transcript.
    :rtype: hl.Table
    """
    logger.info(f"Grouping variants by {*groupings}...")
    group_ht = context_ht.group_by(*groupings).aggregate(variant_count=hl.agg.count())

    logger.info("Getting coverage and plateau model corrections...")
    group_ht = group_ht.annotate(
        coverage_correction=get_coverage_correction_expr(
            group_ht.exome_coverage, group_ht.coverage_model
        )
    )
    model = get_plateau_model(context_ht.locus, context_ht.cpg, context_ht.globals)
    group_ht = group_ht.annotate(
        adj_cov=(
            ((group_ht.variant_count * model[1]) + model[0])
            * group_ht.coverage_correction
        )
    )

    logger.info("Annotating context HT with adjusted variant counts...")
    context_ht = context_ht.annotate(
        adj_cov=group_ht[context_ht.row.select(*groupings)].adj_cov
    )
    return context_ht.annotate(mu_adj=context_ht.mu_snp * context_ht.adj_cov)


def calculate_exp_per_transcript(
    context_ht: hl.Table, groupings: List[str] = ["transcript", "cpg", "exome_coverage"]
) -> hl.Table:
    """
    Calculates the total number of expected variants per transcript.

    Expected variants count is mutation rate per SNP adjusted based on CpG status and coverage.

    .. note::
        Expects:
        - context_ht is annotated with mutation rate (`mu_snp`).
        - context_ht contains coverage and plateau models in global annotations (`coverage_model`, `plateau_models`).

    :param hl.Table context_ht: Context Table.
    :param List[str] groupings: List of Table fields used to group Table to adjust mutation rate. 
        Table must be annotated with these fields. Default fields are transcript, cpg, and exome_coverage.
    :return: Table grouped by transcript with expected variant counts per transcript.
    :rtype: hl.Table
    """
    logger.info(f"Grouping by {*groupings}...")
    group_ht = context_ht.group_by(*groupings,).aggregate(
        mu_agg=hl.agg.sum(context_ht.mu_snp)
    )

    logger.info("Adjusting aggregated mutation rate with plateau model...")
    model = get_plateau_model(context_ht.locus, context_ht.cpg, context_ht.globals)
    group_ht = group_ht.transmute(mu_adj=group_ht.mu_agg * model[1] + model[0])

    logger.info(
        "Adjusting aggregated mutation rate with coverage correction to get expected counts..."
    )
    group_ht = group_ht.annotate(
        coverage_correction=get_coverage_correction_expr(
            group_ht.exome_coverage, group_ht.coverage_model
        ),
    )
    group_ht = group_ht.annotate(_exp=(group_ht.mu_agg * group_ht.coverage_correction))

    logger.info("Getting expected counts per transcript and returning...")
    return group_ht.group_by("transcript").aggregate(
        total_exp=hl.agg.sum(group_ht._exp)
    )


def get_obs_exp_expr(
    cond_expr: hl.expr.BooleanExpression,
    obs_expr: hl.expr.Int64Expression,
    exp_expr: hl.expr.Float64Expression,
) -> hl.expr.Float64Expression:
    """
    Returns observed/expected annotation based on inputs.

    Caps observed/expected value at 1.

    Function can generate observed/expected values across the entire transcript or section of a transcript depending on inputs.
    Function can also generate 'forward' (moving from smaller to larger positions") or 'reverse' (moving from larger to smaller positions)
    section obs/exp values.

    .. note::
        `cond_expr` should vary depending on size/direction of section being annotated.  

    :param hl.expr.BooleanExpression cond_expr: Condition to check prior to adding obs/exp expression.
    :param hl.expr.Int64Expression obs_expr: Expression containing number of observed variants.
    :param hl.expr.Float64Expression exp_expr: Expression containing number of expected variants.
    :return: Observed/expected expression.
    :rtype: hl.expr.Float64Expression
    """
    return hl.or_missing(cond_expr, hl.min(obs_expr / exp_expr, 1))


def get_cumulative_scan_expr(
    search_expr: hl.expr.StringExpression,
    observed_expr: hl.expr.Int64Expression,
    mu_expr: hl.expr.Float64Expression,
    plateau_model: Tuple[hl.expr.Float64Expression, hl.expr.Float64Expression],
    coverage_correction_expr: hl.expr.Float64Expression,
) -> hl.expr.StructExpression:
    """
    Creates struct with cumulative number of observed and expected variants.

    .. note::
        This function can produce the scan when searching for the first break or when searching for a second additional break.
            - When searching for the first break, this function should group by the transcript name (e.g., 'ENST00000255882').
            - When searching for an additional break, this function should group by the section of the transcript 
                (e.g., 'first' for before the first breakpoint or 'second' for after the first breakpoint).

    :param hl.expr.StringExpression search_expr: Expression containing transcript if searching for first break.
        Otherwise, expression containing transcript section if searching for second additional break.
    :param hl.expr.Float64Expression mu_expr: Mutation rate expression.
    :param hl.expr.Int64Expression observed_expr: Observed variants expression.
    :return: Struct containing the cumulative number of observed and expected variants.
    :param Tuple[hl.expr.Float64Expression, hl.expr.Float64Expression] plateau_model: Model to determine adjustment to mutation rate
        based on locus type and CpG status.
    :param hl.expr.Float64Expression coverage correction: Expression containing coverage correction necessary to adjust
        expected variant counts at low coverage sites.
    :return: Struct containing scan expressions for cumulative observed and expected variant counts.
    :rtype: hl.expr.StructExpression
    """
    return hl.struct(
        cumulative_obs=hl.scan.group_by(search_expr, hl.scan.sum(observed_expr)),
        cumulative_exp=hl.scan.group_by(
            search_expr,
            (plateau_model[1] * hl.scan.sum(mu_expr) + plateau_model[0])
            * coverage_correction_expr,
        ),
    )


def get_reverse_obs_exp_expr(
    cond_expr: hl.expr.BooleanExpression,
    total_obs_expr: hl.expr.Int64Expression,
    total_exp_expr: hl.expr.Float64Expression,
    scan_obs_expr: Dict[hl.expr.StringExpression, hl.expr.Int64Expression],
    scan_exp_expr: Dict[hl.expr.StringExpression, hl.expr.Float64Expression],
) -> hl.expr.StructExpression:
    """
    Returns the "reverse" section observed and expected variant counts.

    The reverse counts are the counts moving from larger to smaller positions 
    (backwards from the end of the transcript back to the beginning of the transcript).
    reverse value = total value - cumulative value

    .. note::
        This function is designed to run on one transcript at a time.

    :param hl.expr.BooleanExpression cond_expr: Conditional expression to check before calculating reverse observed or expected value.
        Should be that the cumulative scan expression length isn't 0 when searching for the first break, or
        that the length of the cumulative scan expression length is 2 when searching for an additional break.
    :param hl.expr.Int64Expression total_obs_expr: Expression containing total number of observed variants for transcript.
    :param hl.expr.Float64Expression total_exp_expr: Expression containing total number of expected variants for transcript.
    :param Dict[hl.expr.StringExpression, hl.expr.Int64Expression] scan_obs_expr: Expression containing cumulative number of observed variants for transcript.
    :param Dict[hl.expr.StringExpression, hl.expr.Float64Expression] scan_expr_expr: Expression containing cumulative number of expected variants for transcript.
    :return: Struct with reverse observed and expected variant counts.
    :rtype: hl.expr.StructExpression
    """
    return hl.struct(
        obs=hl.or_missing(cond_expr, total_obs_expr - scan_obs_expr),
        exp=hl.or_missing(cond_expr, total_exp_expr - scan_exp_expr),
    )


def get_fwd_exprs(
    ht: hl.Table,
    search_field: str,
    observed_expr: hl.expr.Int64Expression,
    mu_expr: hl.expr.Float64Expression,
    locus_expr: hl.expr.LocusExpression,
    cpg_expr: hl.expr.BooleanExpression,
    globals_expr: hl.expr.StructExpression,
    coverage_correction_expr: hl.expr.Float64Expression,
) -> hl.Table:
    """
    Calls `get_cumulative_scan_expr and `get_obs_exp_expr` to add the forward section cumulative observed, expected, and observed/expected values.

    .. note::
        'Forward' refers to moving through the transcript from smaller to larger chromosomal positions.

    :param hl.Table ht: Input Table.
    :param str search_field: Name of field to group by prior to running scan. Should be 'transcript' if searching for the first break.
        Otherwise, should be transcript section if searching for additional breaks.
    :param hl.expr.Int64Expression observed_expr: Expression containing number of observed variants per site.
    :param hl.expr.Float64Expression mu_expr: Expression containing mutation rate probability of site.
    :param hl.expr.LocusExpression locus_expr: Locus expression.
    :param hl.expr.BooleanExpression cpg_expr: Expression showing whether site is a CpG site.
    :param hl.expr.StructExpression globals_expr: Expression containing global annotations of context HT. Must contain plateau models as annotations.
    :param hl.expr.Float64Expression coverage_correction_expr: Expression containing coverage correction necessary to adjust
        expected variant counts at low coverage sites.
    :return: Table with forward values annotated
    :rtype: hl.Table
    """

    ht = ht.annotate(
        scan_counts=get_cumulative_scan_expr(
            search_expr=ht[search_field],
            observed_expr=observed_expr,
            mu_expr=mu_expr,
            plateau_model=get_plateau_model(locus_expr, cpg_expr, globals_expr),
            coverage_correction_expr=coverage_correction_expr,
        )
    )
    if search_field == "transcript":
        ht = ht.annotate(cond_expr=hl.len(ht.scan_counts.cumulative_obs) != 0)
    else:
        ht = ht.annotate(cond_expr=hl.len(ht.scan_counts.cumulative_obs) > 1)

    return ht.annotate(
        forward_obs_exp=get_obs_exp_expr(
            ht.cond_expr,
            ht.scan_counts.cumulative_observed[ht[search_field]],
            ht.scan_counts.cumulative_expected[ht[search_field]],
        )
    )


def get_reverse_exprs(
    ht: hl.Table,
    cond_expr: hl.expr.BooleanExpression,
    total_obs_expr: hl.expr.Int64Expression,
    total_exp_expr: hl.expr.Float64Expression,
    scan_obs_expr: Dict[hl.expr.StringExpression, hl.expr.Int64Expression],
    scan_exp_expr: Dict[hl.expr.StringExpression, hl.expr.Float64Expression],
) -> hl.Table:
    """
    Calls `get_reverse_obs_exp_expr` and `get_obs_exp_expr` to add the reverse section cumulative observed, expected, and observed/expected values.

    .. note::
        'Reverse' refers to moving through the transcript from larger to smaller chromosomal positions.

    :param hl.Table ht: Input Table.
    :param hl.expr.BooleanExpression cond_expr: Condition to check before calculating reverse values.
    :param hl.expr.Int64Expression total_obs_expr: Expression containing total number of observed variants per transcript (if searching for first break)
        or per section (if searching for additional breaks).
    :param hl.expr.Float64Expression total_exp_expr: Expression containing total number of expected variants per transcript (if searching for first break)
        or per section (if searching for additional breaks).
    :param Dict[hl.expr.StringExpression, hl.expr.Int64Expression] scan_obs_expr: Expression containing cumulative number of observed variants per transcript
        (if searching for first break) or per section (if searching for additional breaks).
    :param Dict[hl.expr.StringExpression, hl.expr.Float64Expression] scan_exp_expr: Expression containing cumulative number of expected variants per transcript
        (if searching for first break) or per section (if searching for additional breaks).
    :return: Table with reverse values annotated
    :rtype: hl.Table
    """
    # reverse value = total value - cumulative value
    ht = ht.annotate(
        reverse=get_reverse_obs_exp_expr(
            cond_expr=cond_expr,
            total_obs_expr=total_obs_expr,
            total_exp_expr=total_exp_expr,
            scan_obs_expr=scan_obs_expr,
            scan_exp_expr=scan_exp_expr,
        )
    )

    # Set reverse o/e to missing if reverse expected value is 0 (to avoid NaNs)
    return ht.annotate(
        reverse_obs_exp=get_obs_exp_expr(
            (ht.reverse_counts.exp != 0), ht.reverse.obs, ht.reverse.exp
        )
    )


def get_dpois_expr(
    cond_expr: hl.expr.BooleanExpression,
    section_oe_expr: hl.expr.Float64Expression,
    obs_expr: Union[
        Dict[hl.expr.StringExpression, hl.expr.Int64Expression], hl.expr.Int64Expression
    ],
    exp_expr: Union[
        Dict[hl.expr.StringExpression, hl.expr.Float64Expression],
        hl.expr.Float64Expression,
    ],
) -> hl.expr.StructExpression:
    """
    Calculates null and alt values in preparation for chi-squared test to find significant breaks.

    All parameter values depend on the direction of calculation (forward/reverse) and 
    number of breaks (searching for first break or searching for additional break).

    For forward null/alts, values for obs_expr and and exp_expr should be:
        - Expression containing cumulative numbers for entire transcript.
        - Expression containing cumulative numbers for section of transcript 
            between the beginning or end of the transcript and the first breakpoint.
    For reverse null/alts, values for obs_expr and and exp_expr should be:
        - Reverse counts for entire transcript.
        - Reverse counts for section of transcript.
    
    For forward null/alts, values for overall_oe_expr and section_oe_expr should be:
        - Expression containing observed/expected value for entire transcript and
            expression containing observed/expected value calculated on cumulative observed and expected
            variants at each position.
        - Expression containing observed/expected value for section of transcript.
    For reverse null/alts, values for overall_oe_expr and section_oe_expr should be:
        - Expression containing observed/expected value for entire transcript and
            expression containing observed/expected value calculated on reverse observed variants value
            (total observed - cumulative observed count).
        - Expression containing observed/expected value for section of transcript and 
            expression containing reverse observed/expected value for section of transcript.

    For forward null/alts, cond_expr should check:
        - That the length of the obs_expr isn't 0 when searching for the first break.
        - That the length of the obs_expr is 2 when searching for a second additional break.
    For reverse null/alts, cond_expr should check:
        - That the reverse observed value for the entire transcript is defined when searching for the first break.
        - That the reverse observed value for the section between the first breakpoint and the end of the transcript
             is defined when searching for a second additional break.

    :param hl.expr.BooleanExpression cond_expr: Conditional expression to check before calculating null and alt values.
    :param hl.expr.Float64Expression section_oe_expr: Expression of section observed/expected value.
    :param Union[Dict[hl.expr.StringExpression, hl.expr.Int64Expression], hl.expr.Int64Expression] obs_expr: Expression containing observed variants count.
    :param Union[Dict[hl.expr.StringExpression, hl.expr.Float64Expression], hl.expr.Float64Expression] exp_expr: Expression containing expected variants count.
    :return: Struct containing forward or reverse null and alt values (either when searching for first or second break).
    :rtype: hl.expr.StructExpression
    """
    return hl.or_missing(cond_expr, hl.dpois(obs_expr, exp_expr * section_oe_expr))


def get_section_expr(dpois_expr: hl.expr.ArrayExpression,) -> hl.expr.Float64Expression:
    """
    Builds null or alt model by multiplying all section null or alt distributions.

    For example, when checking for the first break in a transcript, the transcript is broken into two sections:
    pre-breakpoint and post-breakpoint. Each section's null and alt distributions must be multiplied 
    to later test the significance of the break.

    :param hl.expr.ArrayExpression dpois_expr: ArrayExpression that contains all section nulls or alts. 
        Needs to be reduced to single float by multiplying each number in the array.
    :return: Overall section distribution.
    :rtype: hl.expr.Float64Expression
    """
    return hl.fold(lambda i, j: i * j, 1, dpois_expr)


def search_for_break(
    context_ht: hl.Table,
    obs_ht: hl.Table,
    exp_ht: hl.Table,
    search_field: hl.expr.StringExpression,
    prediction_flag: Tuple[float, int],
    chisq_threshold: float,
    group_by_transcript: bool,
) -> Union[hl.Table, None]:
    """
    Searches for breakpoints in a transcript. 

    Currently designed to run one transcript at a time. Expects input Table to be filtered to single transcript.

    Expects context HT to contain the following fields:
        - locus
        - alleles
        - transcript or section
        - coverage (median)
        - mu
    Also expects:
        - multiallelic variants in context HT have been split.
        - context HT is autosomes/PAR only, X non-PAR only, or Y non-PAR only.

    Returns HT filtered to lines with maximum chisq if chisq >= max_value, otherwise returns None.

    :param hl.Table context_ht: Context Table. 
    :param hl.Table obs_ht: Table grouped by transcript with observed variant counts per transcript.
        Expects observed counts field to be named `observed`.
    :param hl.Table exp_ht: Table grouped by transcript with expected variant counts per transcript.
        Expects expected counts field to be named `expected`.
    :param hl.expr.StringExpression search_field: Field of table to search. Value should be either 'transcript' or 'section'. 
    :param Tuple(float, int) prediction_flag: Adjustments to mutation rate based on chromosomal location
        (autosomes/PAR, X non-PAR, Y non-PAR). 
        E.g., prediction flag for autosomes/PAR is (0.4190964, 11330208)
    :param float chisq_threshold: Chisq threshold for significance. 
        Value should be 10.8 (single break) and 13.8 (two breaks) (values from ExAC RMC code).
    :param bool group_by_transcript: Whether function should group by transcript. Should be True if searching 
        for first break and False to search for an additional break. 
    :return: Table filtered to rows with maximum chisq value IF max chisq is larger than chisq_threshold.
        Otherwise, returns None.
    :rtype: Union[hl.Table, None]
    """
    ht = annotate_observed_expected(context_ht, obs_ht, exp_ht, group_by_transcript)

    logger.info(
        "Annotating HT with cumulative expected/observed counts per transcript..."
    )
    ht = ht.annotate(
        scan_counts=get_cumulative_scan_expr(
            search_expr=ht[search_field],
            observed_expr=ht.observed,
            mu_expr=ht.mu,
            prediction_flag=prediction_flag,
            coverage_correction=get_coverage_correction(ht.coverage),
        )
    )

    logger.info("Annotating HT with forward scan section observed/expected value...")
    # NOTE: Capping observed/expected values at 1
    ht = ht.annotate(
        obs_exp=hl.or_missing(
            hl.len(ht.scan_counts.cumulative_observed) != 0,
            hl.min(
                ht.scan_counts.cumulative_observed[search_field]
                / ht.scan_counts.cumulative_expected[search_field],
                1,
            ),
        ),
    )

    logger.info("Adding forward scan section nulls and alts...")
    # Add forwards sections (going through positions from smaller to larger)
    # section_null = stats.dpois(section_obs, section_exp*overall_obs_exp)[0]
    # section_alt = stats.dpois(section_obs, section_exp*section_obs_exp)[0]
    ht = ht.annotate(
        forward=get_null_alt_expr(
            cond_expr=hl.len(ht.cumulative_observed) != 0,
            overall_oe_expr=ht.overall_obs_exp,
            section_oe_expr=ht.obs_exp,
            obs_expr=ht.cumulative_observed[search_field],
            exp_expr=ht.cumulative_expected[search_field],
        )
    )

    logger.info("Adding reverse section observeds and expecteds...")
    # reverse value = total value - cumulative value
    ht = ht.annotate(
        reverse_counts=get_reverse_obs_exp_expr(
            cond_expr=hl.len(ht.cumulative_observed) != 0,
            total_obs_expr=ht.total_obs,
            total_exp_expr=ht.total_exp,
            scan_obs_expr=ht.cumulative_observed[search_field],
            scan_exp_expr=ht.cumulative_expected[search_field],
        )
    )

    # Set reverse o/e to missing if reverse expected value is 0 (to avoid NaNs)
    # Also cap reverse observed/expected at 1
    ht = ht.annotate(
        reverse_obs_exp=hl.or_missing(
            ht.reverse_counts.exp != 0,
            hl.min(ht.reverse_counts.obs / ht.reverse_counts.exp, 1),
        )
    )

    logger.info("Adding reverse section nulls and alts...")
    ht = ht.annotate(
        reverse=get_null_alt_expr(
            cond_expr=hl.len(ht.scan_counts.cumulative_observed) != 0,
            overall_oe_expr=ht.overall_obs_exp,
            section_oe_expr=ht.obs_exp,
            obs_expr=ht.scan_counts.cumulative_observed[search_field],
            exp_expr=ht.scan_counts.cumulative_expected[search_field],
        )
    )

    logger.info("Multiplying all section nulls and all section alts...")
    # Kaitlin stores all nulls/alts in section_null and section_alt and then multiplies
    # e.g., p1 = prod(section_null_ps)
    ht = ht.annotate(
        section_null=ht.forward.null * ht.reverse.null,
        section_alt=ht.forward.alt * ht.reverse.alt,
    )

    logger.info("Adding chisq value and getting max chisq...")
    ht = ht.annotate(chisq=(2 * (hl.log(ht.section_alt) - hl.log(ht.section_null))))

    # "The default chi-squared value for one break to be considered significant is
    # 10.8 (p ~ 10e-3) and is 13.8 (p ~ 10e-4) for two breaks. These currently cannot
    # be adjusted."
    max_chisq = ht.aggregate(hl.agg.max(ht.chisq))
    if max_chisq >= chisq_threshold:
        return ht.filter(ht.chisq == max_chisq)

    return None


def process_transcripts(ht: hl.Table, chisq_threshold: float):
    """
    Annotates each position in Table with whether that position is a significant breakpoint.

    Also annotates input Table with cumulative observed, expected, and observed/expected values
    for both forward (moving from smaller to larger positions) and reverse (moving from larger to 
    smaller positions) directions.

    Expects input Table to have the following fields:
        - locus
        - alleles
        - mu_snp
        - transcript
        - observed
        - expected
        - coverage_correction
        - cpg
    Also expects:
        - multiallelic variants in context HT have been split.
        - Global annotations contains plateau models.

    :param hl.Table ht: Input Table. annotated with observed and expected variants counts per transcript.
    :param float chisq_threshold: Chi-square significance threshold. 
        Value should be 10.8 (single break) and 13.8 (two breaks) (values from ExAC RMC code).
    :return: Table with cumulative observed, expected, and observed/expected values annotated for forward and reverse directions.
        Table also annotated with boolean for whether each position is a breakpoint.
    :rtype: hl.Table
    """
    logger.info(
        "Annotating HT with cumulative observed and expected counts for each transcript...\n"
        "(transcript-level forwards (moving from smaller to larger positions) values)"
    )
    ht = get_fwd_exprs(
        ht=ht,
        search_field="transcript",
        observed_expr=ht.observed,
        mu_expr=ht.mu_snp,
        locus_expr=ht.locus,
        cpg_expr=ht.cpg,
        globals_expr=ht.globals,
        coverage_correction_expr=ht.coverage_correction,
    )
    logger.info(
        "Annotating HT with reverse observed and expected counts for each transcript...\n"
        "(transcript-level reverse (moving from larger to smaller positions) values)"
    )
    # cond_expr here skips the first line of the HT, as the cumulative values
    # of the first line will always be empty when using a scan
    ht = get_reverse_exprs(
        ht=ht,
        cond_expr=hl.len(ht.scan_counts.cumulative_obs) != 0,
        total_obs_expr=ht.total_obs,
        total_exp_expr=ht.total_exp,
        scan_obs_expr=ht.scan_counts.cumulative_obs[ht.transcript],
        scan_exp_expr=ht.scan_counts.cumulative_exp[ht.transcript],
    )

    return search_for_break(ht, "transcript", chisq_threshold)


def process_sections(ht: hl.Table, chisq_threshold: float):
    """
    Search for breaks within given sections of a transcript. 

    Expects that input Table has the following annotations:
        - cpg
        - observed
        - mu_snp
        - coverage_correction
        - section
    Also assumes that Table's globals contain plateau models.

    :param hl.Table ht: Input Table.
    :param float chisq_threshold: Chi-square significance threshold. 
        Value should be 10.8 (single break) and 13.8 (two breaks) (values from ExAC RMC code).
    :return: Table annotated with whether position is a breakpoint. 
    :rtype: hl.Table
    """
    logger.info(
        "Getting total observed and expected counts for each transcript section..."
    )
    ht = ht.annotate(plateau_model=get_plateau_model(ht.locus, ht.cpg, ht.globals))
    section_group = ht.group_by(ht.section).aggregate(
        obs=hl.agg.sum(ht.observed),
        exp=(hl.agg.sum(ht.mu_snp) * ht.plateau_model[1] + ht.plateau_model[0])
        * ht.coverage_correction,
    )
    ht = ht.annotate(
        break_obs=section_group[ht.section].obs,
        break_exp=section_group[ht.section].exp,
    )

    logger.info(
        "Annotating HT with cumulative observed and expected counts for each transcript section..."
    )
    ht = get_fwd_exprs(
        ht=ht,
        search_field="section",
        observed_expr=ht.observed,
        mu_expr=ht.mu_snp,
        locus_expr=ht.locus,
        cpg_expr=ht.cpg,
        globals_expr=ht.globals,
        coverage_correction_expr=ht.coverage_correction,
    )

    logger.info(
        "Annotating HT with reverse observed and expected counts for each transcript section..."
    )
    # cond_expr here skips the first line of the HT, as the cumulative values
    # of the first line will always be empty when using a scan
    ht = get_reverse_exprs(
        ht=ht,
        # This cond expression searches for the start of the second section
        cond_expr=hl.len(ht.scan_counts.cumulative_obs) > 1,
        total_obs_expr=ht.break_obs,
        total_exp_expr=ht.break_exp,
        scan_obs_expr=ht.scan_counts.cumulative_obs[ht.section],
        scan_exp_expr=ht.scan_counts.cumulative_exp[ht.section],
    )
    return search_for_break(ht, "section", chisq_threshold)


def process_additional_breaks(ht: hl.Table, chisq_threshold: float) -> hl.Table:
    """
    Search for additional breaks in a transcript after finding one significant break.
    
    Expects that input Table has the following annotations:
        - cpg
        - observed
        - mu_snp
        - coverage_correction
        - transcript
    Also assumes that Table's globals contain plateau models.

    :param hl.Table ht: Input Table.
    :param float chisq_threshold: Chi-square significance threshold. 
        Value should be 10.8 (single break) and 13.8 (two breaks) (values from ExAC RMC code).
    :return: Table annotated with whether position is a breakpoint. 
    :rtype: hl.Table
    """
    # TODO: generalize function so that it can search for more than one additional break
    logger.info("Getting set of transcripts with one break...")
    first_break_ht = ht.filter(ht.is_break)
    first_break_ht = first_break_ht.select().key_by("transcript")
    transcripts = first_break_ht.aggregate(
        hl.agg.collect_as_set(first_break_ht.transcript), _localize=False
    )

    logger.info("Filtering to transcripts with one break...")
    # Also rename scans from first break as these will be overwritten when searching for additional break
    ht = ht.filter(transcripts.contains(ht.transcript))
    ht = ht.rename(
        {
            "scan_counts": "first_scan_counts",
            "reverse": "first_reverse",
            "forward_obs_exp": "first_forward_obs_exp",
            "reverse_obs_exp": "first_reverse_obs_exp",
            "is_break": "is_first_break",
        }
    )

    logger.info(
        "Splitting each transcript into two sections: pre first break and post..."
    )
    ht = ht.annotate(
        section=hl.if_else(
            ~ht.is_first_break,
            hl.if_else(
                ht.locus.position > first_break_ht[ht.transcript].locus.position,
                hl.format("%s_%s", ht.transcript, "post"),
                hl.format("%s_%s", ht.transcript, "pre"),
            ),
            # If position is breakpoint, add to second section ("post")
            # this is because the first position of a scan is always missing anyway
            hl.format("%s_%s", ht.transcript, "post"),
        )
    )
    return process_sections(ht, chisq_threshold)


def get_avg_bases_between_mis(ht: hl.Table, build: str) -> int:
    """
    Returns average number of bases between observed missense variation.

    For example, if the total number of bases is 30, and the total number of missense variants is 10,
    this function will return 3.

    This function is used to determine the minimum size window to check for significant missense depletion
    when searching for two simultaneous breaks.

    :param hl.Table ht: Input gnomAD Table.
    :return: Average number of bases between observed missense variants, rounded to the nearest integer,
    :rtype: int
    """
    logger.info("Getting total number of bases in the exome (based on GENCODE)...")
    total_bases = get_exome_bases(build)

    logger.info(
        "Filtering to missense variants in canonical protein coding transcripts..."
    )
    ht = filter_to_missense(ht)
    total_variants = ht.count()
    return round(total_bases / total_variants)


def search_for_two_breaks(
    ht: hl.Table,
    exome_ht: hl.Table,
    transcript: str,
    chisq_threshold: float,
    num_obs_var: int = 10,
) -> Union[Tuple(float, Tuple(int, int)), None]:
    """
    Searches for evidence of constraint within a set window size/number of base pairs.

    Function is designed to search in transcripts that didn't have one single significant break.
    Currently designed to one run one transcript at a time.

    Assumes that:
        - Input Table has a field named 'transcript'.

    :param hl.Table ht: Input Table.
    :param hl.Table exome_ht: Table containing variants from gnomAD exomes.
    :param str transcript: Transcript of interest.
    :param float chisq_threshold: Chi-square significance threshold. 
        Value should be 10.8 (single break) and 13.8 (two breaks) (values from ExAC RMC code).
    :param int num_obs_var: Number of observed variants. Used when determining the window size for simultaneous breaks. 
        Default is 10, meaning that the window size for simultaneous breaks is the average number of base pairs required to see 10 observed variants.
    :return: Tuple of largest chi-square value and breakpoint positions if significant break was found. Otherwise, None.
    :rtype: Union[hl.Table, None]
    """
    break_size = (
        get_avg_bases_between_mis(
            exome_ht, get_reference_genome(exome_ht.head(1).locus).name
        )
        * num_obs_var
    )
    logger.info(
        f"Number of bases to search for constraint (size for simultaneous breaks): {break_size}"
    )

    # I don't think there's any way to search for simultaneous breaks without a loop?
    ht = ht.filter(ht.transcript == transcript)
    start_pos = ht.head(1).take(1).locus.position
    end_pos = ht.tail(1).take(1).locus.position
    best_chisq = 0
    breakpoints = ()

    while start_pos < end_pos:
        ht = ht.annotate(
            section=hl.case()
            # Position is within window if pos is larger than start_pos and
            # less than start_pos + window size, then pos is within window
            .when(
                (ht.locus.position >= start_pos)
                & (ht.locus.position < (start_pos + break_size)),
                hl.format("%s_%s", ht.transcript, "window"),
            )
            # If pos < start_pos, pos is outside of window and pre breaks
            .when(
                ht.locus.position < start_pos, hl.format("%s_%s", ht.transcript, "pre")
            )
            # Otherwise, pos is outside window and post breaks
            .default(hl.format("%s_%s", ht.transcript, "post"))
        )
        new_ht = process_sections(ht, chisq_threshold)

        # Check if found break
        if new_ht.aggregate(hl.agg.counter(new_ht.is_break) > 0):
            max_chisq = new_ht.aggregate(hl.agg.max(new_ht.max_chisq))
            if (max_chisq > best_chisq) and (max_chisq >= chisq_threshold):
                breakpoints = (start_pos, (start_pos + break_size) - 1)

        start_pos += 1

    if best_chisq != 0:
        return (best_chisq, breakpoints)
    return None
