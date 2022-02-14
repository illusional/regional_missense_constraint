import argparse
import logging

import hail as hl

from gnomad.resources.resource_utils import DataException
from gnomad.utils.file_utils import file_exists
from gnomad.utils.reference_genome import get_reference_genome
from gnomad.utils.slack import slack_notifications

from rmc.resources.basics import (
    constraint_prep,
    LOGGING_PATH,
    multiple_breaks,
    # no_breaks,
    not_one_break,
    not_one_break_grouped,
    one_break,
    simul_break,
    temp_path,
)
from rmc.resources.grch37.exac import filtered_exac
from rmc.resources.grch37.gnomad import (
    constraint_ht,
    filtered_exomes,
    processed_exomes,
    prop_obs_coverage,
)
from rmc.resources.grch37.reference_data import processed_context
from rmc.resources.resource_utils import GNOMAD_VER, MISSENSE
from rmc.slack_creds import slack_token
from rmc.utils.constraint import (
    calculate_exp_per_transcript,
    calculate_observed,
    fix_xg,
    get_fwd_exprs,
    GROUPINGS,
    process_additional_breaks,
    process_transcripts,
    search_for_two_breaks,
)
from rmc.utils.generic import (
    filter_to_region_type,
    generate_models,
    get_avg_bases_between_mis,
    get_coverage_correction_expr,
    get_outlier_transcripts,
    keep_criteria,
    process_context_ht,
    process_vep,
)


logging.basicConfig(
    format="%(asctime)s (%(name)s %(lineno)s): %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger("regional_missense_constraint")
logger.setLevel(logging.INFO)


def main(args):
    """Call functions from `constraint.py` to calculate regional missense constraint."""
    exac = args.exac

    try:
        if args.pre_process_data:
            hl.init(log="/RMC_pre_process.log")
            # TODO: Add code to create annotations necessary for constraint_flag_expr and filter transcripts prior to running constraint
            logger.warning("Code currently only processes b37 data!")
            logger.info(
                "Filtering gnomAD exomes HT to missense variants in canonical transcripts only..."
            )
            exome_ht = processed_exomes.ht()
            exome_ht = process_vep(exome_ht, filter_csq=True, csq=MISSENSE)

            # Move nested annotations into top level annotations
            exome_ht = exome_ht.select(
                ac=exome_ht.freq[0].AC,
                af=exome_ht.freq[0].AF,
                pass_filters=exome_ht.pass_filters,
                exome_coverage=exome_ht.coverage.exomes.median,
                transcript_consequences=exome_ht.transcript_consequences,
            )

            logger.info("Preprocessing reference fasta (context) HT...")
            context_ht = process_context_ht("GRCh37", args.trimers)

            logger.info(
                "Filtering context HT to all sites not found in gnomAD exomes + all rare, covered sites in gnomAD"
            )
            exome_join = exome_ht[context_ht.key]
            context_ht = context_ht.filter(
                hl.is_missing(exome_join) | keep_criteria(exome_join)
            )
            # NOTE: need to repartition here to desired number of partitions!
            # NOTE: should use ~30k-40k partitions
            context_ht = context_ht.repartition(args.n_partitions)
            context_ht.write(processed_context.path, overwrite=args.overwrite)

            exome_ht = exome_ht.filter(keep_criteria(exome_ht))
            exome_ht.write(filtered_exomes.path, overwrite=args.overwrite)

            logger.info("Done preprocessing files")

        if args.prep_for_constraint:
            hl.init(log="/RMC_prep_for_constraint.log")
            logger.info("Reading in exome HT...")
            if exac:
                exome_ht = filtered_exac.ht()

            else:
                exome_ht = filtered_exomes.ht()

            logger.info("Reading in context HT...")
            context_ht = processed_context.ht()

            logger.info("Building plateau and coverage models...")
            coverage_ht = prop_obs_coverage.ht()
            coverage_x_ht = hl.read_table(
                prop_obs_coverage.path.replace(".ht", "_x.ht")
            )
            coverage_y_ht = hl.read_table(
                prop_obs_coverage.path.replace(".ht", "_y.ht")
            )

            (
                coverage_model,
                plateau_models,
                plateau_x_models,
                plateau_y_models,
            ) = generate_models(
                coverage_ht, coverage_x_ht, coverage_y_ht, trimers=args.trimers
            )

            context_ht = context_ht.annotate_globals(
                plateau_models=plateau_models,
                plateau_x_models=plateau_x_models,
                plateau_y_models=plateau_y_models,
                coverage_model=coverage_model,
            )
            context_ht = context_ht.annotate(
                coverage_correction=get_coverage_correction_expr(
                    context_ht.exome_coverage, coverage_model, args.high_cov_cutoff,
                )
            )

            if not args.skip_calc_oe:
                logger.info(
                    "Adding coverage correction to mutation rate probabilities..."
                )
                context_ht = context_ht.annotate(
                    raw_mu_snp=context_ht.mu_snp,
                    mu_snp=context_ht.mu_snp
                    * get_coverage_correction_expr(
                        context_ht.exome_coverage, context_ht.coverage_model
                    ),
                )

                logger.info(
                    "Creating autosomes-only, chrX non-PAR-only, and chrY non-PAR-only HT versions..."
                )
                context_x_ht = filter_to_region_type(context_ht, "chrX")
                context_y_ht = filter_to_region_type(context_ht, "chrY")
                context_auto_ht = filter_to_region_type(context_ht, "autosomes")

                logger.info("Calculating expected values per transcript...")
                exp_ht = calculate_exp_per_transcript(
                    context_auto_ht, locus_type="autosomes", groupings=GROUPINGS,
                )
                exp_x_ht = calculate_exp_per_transcript(
                    context_x_ht, locus_type="X", groupings=GROUPINGS,
                )
                exp_y_ht = calculate_exp_per_transcript(
                    context_y_ht, locus_type="Y", groupings=GROUPINGS,
                )
                exp_ht = exp_ht.union(exp_x_ht).union(exp_y_ht)

                logger.info(
                    "Fixing expected values for genes that span PAR and nonPAR regions..."
                )
                # Adding a sum here to make sure that genes like XG that span PAR/nonPAR regions
                # have correct total expected values
                exp_ht = exp_ht.group_by(transcript=exp_ht.transcript).aggregate(
                    total_exp=hl.agg.sum(exp_ht.expected),
                    total_mu=hl.agg.sum(exp_ht.mu_agg),
                )

                logger.info(
                    "Aggregating total observed variant counts per transcript..."
                )
                obs_ht = calculate_observed(exome_ht)

            else:
                logger.warning(
                    "Using observed and expected values calculated on gnomAD v2.1.1 exomes..."
                )
                exp_ht = (
                    constraint_ht.ht()
                    .key_by("transcript")
                    .select("obs_mis", "exp_mis", "oe_mis")
                )

                # Filter to canonical transcripts only and rename fields
                exp_ht = exp_ht.filter(exp_ht.canonical)
                exp_ht = exp_ht.transmute(
                    observed=exp_ht.obs_mis,
                    expected=exp_ht.exp_mis,
                    mu_agg=exp_ht.mu_mis,
                )
                obs_ht = exp_ht

            logger.info(
                "Annotating context HT with number of observed and expected variants per site..."
            )
            # Add observed variants to context HT
            context_ht = context_ht.annotate(_obs=exome_ht.index(context_ht.key))
            context_ht = context_ht.transmute(
                observed=hl.int(hl.is_defined(context_ht._obs))
            )

            logger.info(
                "Collecting by key to run constraint per base and not per base-allele..."
            )
            # Context HT is keyed by locus and allele, which means there is one row for every possible missense variant
            # This means that any locus could be present up to three times (once for each possible missense)
            # Collect by key here to ensure all loci are unique
            context_ht = context_ht.key_by("locus", "transcript").collect_by_key()
            context_ht = context_ht.annotate(
                # Collect the mutation rate probabilities at each locus
                mu_snp=hl.sum(context_ht.values.mu_snp),
                # Collect the observed counts for each locus
                # (this includes counts for each possible missense at the locus)
                observed=hl.sum(context_ht.values.observed),
                # Take just the first coverage value, since the locus should have the same coverage across the possible variants
                coverage=context_ht.values.exome_coverage[0],
            )

            logger.info(
                "Annotating total observed and expected values and overall observed/expected value "
                "(capped at 1) per transcript..."
            )
            context_ht = context_ht.annotate(
                total_exp=exp_ht[context_ht.transcript].total_exp,
                total_mu=exp_ht[context_ht.transcript].total_mu,
                total_obs=obs_ht[context_ht.transcript].observed,
            )
            context_ht = context_ht.annotate(
                overall_oe=hl.min(context_ht.total_obs / context_ht.total_exp, 1)
            )

            context_ht = get_fwd_exprs(
                ht=context_ht,
                transcript_str="transcript",
                obs_str="observed",
                mu_str="mu_snp",
                total_mu_str="total_mu",
                total_exp_str="total_exp",
            )

            context_ht = context_ht.write(
                constraint_prep.path, overwrite=args.overwrite
            )

        if args.search_for_first_break:
            hl.init(log="/RMC_first_break.log")

            logger.info("Searching for transcripts with a significant break...")
            context_ht = constraint_prep.ht()
            context_ht = process_transcripts(context_ht, args.chisq_threshold)
            context_ht = context_ht.checkpoint(
                f"{temp_path}/first_break.ht", overwrite=True
            )

            logger.info(
                "Filtering HT to transcripts with one significant break and writing..."
            )
            is_break_ht = context_ht.filter(context_ht.is_break)
            transcripts = is_break_ht.aggregate(
                hl.agg.collect_as_set(is_break_ht.transcript), _localize=False
            )
            one_break_ht = context_ht.filter(
                transcripts.contains(context_ht.transcript)
            )
            one_break_ht = one_break_ht.annotate_globals(
                break_1_transcripts=transcripts
            )
            one_break_ht.write(one_break.path, overwrite=args.overwrite)

            logger.info(
                "Filtering HT to transcripts without a significant break and writing..."
            )
            not_one_break_ht = context_ht.anti_join(one_break_ht)
            not_one_break_ht = not_one_break_ht.drop("values")
            not_one_break_ht.write(not_one_break.path, overwrite=args.overwrite)

        if args.search_for_additional_breaks:
            hl.init(log="/RMC_additional_breaks.log")

            # Set hail flag to avoid method too large and out of memory errors
            hl._set_flags(no_whole_stage_codegen="1")

            logger.info(
                "Searching for additional breaks in transcripts with at least one significant break..."
            )
            context_ht = one_break.ht()

            # Add break_list annotation to context HT
            context_ht = context_ht.annotate(break_list=[context_ht.is_break])
            break_ht = context_ht

            # Start break number counter at 2
            break_num = 2

            while True:
                # Search for additional breaks
                # This technically should search for two additional breaks at a time:
                # this calls `process_sections`, which checks each section of the transcript for a break
                # sections are transcript section pre and post first breakpoint
                break_ht = process_additional_breaks(
                    break_ht, break_num, args.chisq_threshold
                )
                break_ht = break_ht.annotate(
                    break_list=break_ht.break_list.append(break_ht.is_break)
                )
                break_ht = break_ht.checkpoint(
                    f"{temp_path}/break_{break_num}.ht", overwrite=True
                )

                # Filter context HT to lines with break and check for transcripts with at least one additional break
                is_break_ht = break_ht.filter(break_ht.is_break)
                group_ht = is_break_ht.group_by("transcript").aggregate(
                    n=hl.agg.count()
                )
                group_ht = group_ht.filter(group_ht.n >= 1)

                # Exit loop if no additional breaks are found for any transcripts
                if group_ht.count() == 0:
                    break

                # Otherwise, pull transcripts and annotate context ht
                break_ht = break_ht.key_by("locus", "transcript")
                transcripts = group_ht.aggregate(
                    hl.agg.collect_as_set(group_ht.transcript), _localize=False
                )
                globals_annot_expr = {f"break_{break_num}_transcripts": transcripts}
                context_ht = context_ht.annotate_globals(**globals_annot_expr)
                annot_expr = {
                    f"break_{break_num}_chisq": break_ht[context_ht.key].chisq,
                    f"break_{break_num}_null": break_ht[context_ht.key].total_null,
                    f"break_{break_num}_alt": break_ht[context_ht.key].total_alt,
                    "is_break": break_ht[context_ht.key].is_break,
                }
                context_ht = context_ht.annotate(**annot_expr)
                context_ht = context_ht.annotate(
                    break_list=context_ht.break_list.append(context_ht.is_break)
                )

                break_ht = break_ht.filter(transcripts.contains(break_ht.transcript))
                break_num += 1

            context_ht.write(multiple_breaks.path, overwrite=args.overwrite)

        if args.search_for_simul_breaks:

            logger.info(
                "Searching for two simultaneous breaks in transcripts that didn't have \
                a single significant break..."
            )
            not_one_break_grouped_path = (
                "gs://regional_missense_constraint/temp/not_one_break_grouped.ht"
            )
            if not file_exists(not_one_break_grouped_path) or args.create_grouped_ht:
                # Make sure user didn't specify a min obs of zero

                logger.info(
                    "Creating grouped HT with lists of cumulative observed and expected missense values..."
                )
                ht = not_one_break.ht()
                print(ht.count())
                ht = ht.transmute(cumulative_obs=ht.cumulative_obs[ht.transcript])

                if args.min_num_obs == 0:
                    raise DataException(
                        "Minimum number of observed variants must be greater than zero!"
                    )

                # Get number of base pairs needed to observe `num` number of missense variants (on average)
                # This number is used to determine the min_window_size - which is the smallest allowed distance between simultaneous breaks.
                min_window_size = (
                    get_avg_bases_between_mis(
                        get_reference_genome(ht.locus).name,
                        args.get_total_exome_bases,
                        args.get_total_gnomad_missense,
                    )
                    * args.min_num_obs
                )
                logger.info(
                    "Minimum window size (window size needed to observe %i missense variants on average): %i",
                    args.min_num_obs,
                    min_window_size,
                )

                transcript_ht = hl.read_table(
                    "gs://regional_missense_constraint/resources/GRCh37/browser/b37_transcripts.ht"
                )

                """group_ht = ht.group_by("transcript").aggregate(
                    cum_obs=hl.agg.collect(ht.cumulative_obs),
                    cum_exp=hl.agg.collect(ht.cumulative_exp),
                    total_oe=hl.agg.take(ht.overall_oe, 1)[0],
                    positions=hl.sorted(hl.agg.collect(ht.locus.position)),
                )
                """
                """
                group_ht = ht.group_by('transcript').aggregate(max_pos=hl.agg.max(ht.locus.position))
                ht = ht.annotate(
                    cum_obs=hl.scan.group_by(ht.transcript, hl.scan.collect(ht.cumulative_obs)),
                    cum_exp=hl.scan.group_by(ht.transcript, hl.scan.collect(ht.cumulative_exp)),
                    positions=hl.scan.group_by(ht.transcript, hl.scan.collect(ht.locus.position)),
                )
                ht = ht.annotate(max_pos=group_ht[ht.transcript].max_pos)
                ht = ht.filter(ht.locus.position == ht.max_pos)
                ht = ht.annotate(max_idx=hl.len(ht.positions) - 1)
                ht = ht.annotate(
                    transcript_start=transcript_ht[ht.transcript].start,
                    transcript_end=transcript_ht[ht.transcript].stop,
                )
                ht = ht.write(not_one_break_grouped_path, overwrite=True)
                """
                group_ht = ht.group_by("transcript").aggregate(
                    values=hl.sorted(
                        hl.agg.collect(
                            hl.struct(
                                locus=ht.locus,
                                cum_exp=ht.cumulative_exp,
                                cum_obs=ht.cumulative_obs,
                                positions=ht.locus.position,
                            ),
                        ),
                        key=lambda x: x.locus,
                    ),
                    total_oe=hl.agg.take(ht.overall_oe, 1)[0],
                )
                group_ht = group_ht.annotate_globals(min_window_size=min_window_size)
                group_ht = group_ht.annotate(
                    max_idx=hl.len(group_ht.values.positions) - 1
                )

                group_ht = group_ht.annotate(
                    transcript_start=transcript_ht[group_ht.key].start,
                    transcript_end=transcript_ht[group_ht.key].stop,
                )
                group_ht = group_ht.transmute(
                    cum_obs=group_ht.values.cum_obs,
                    cum_exp=group_ht.values.cum_exp,
                    positions=group_ht.values.positions,
                )
                group_ht.write(not_one_break_grouped_path, overwrite=True)

            ht = hl.read_table(not_one_break_grouped_path)
            transcripts = ht.aggregate(hl.agg.collect_as_set(ht.transcript))
            logger.info("Found %i transcripts", len(transcripts))
            ht.describe()

            logger.info("Searching for transcripts with simultaneous breaks...")
            # transcripts = list(transcripts)
            # chunks = [transcripts[x:x+100] for x in range(0, len(transcripts), 100)]
            # temp_hts = []
            # for counter, subset in enumerate(chunks):
            #    logger.info("Working on subset number %i", counter)
            #    temp = ht.filter(hl.literal(subset).contains(ht.transcript))
            #    temp = search_for_two_breaks(temp, args.chisq_threshold)
            #    temp = temp.checkpoint(f"gs://gnomad-tmp/kc/simul_split_{counter}.ht", overwrite=args.overwrite)
            #    temp_hts.append(temp)

            # logger.info("Joining and writing...")
            # ht = temp_hts[0].union(*temp_hts[1:])
            # ht.write("gs://regional_missense_constraint/temp/simul_split_test.ht", overwrite=args.overwrite)

            # ht_head = ht.head(9229)
            # ht_tail = ht.tail(9230)
            # ht_head = search_for_two_breaks(ht_head, args.chisq_threshold)
            # logger.info("Writing out first 9229 rows...")
            # ht_head = ht_head.checkpoint("gs://gnomad-tmp/kc/simul_breaks_9229.ht", overwrite=args.overwrite)

            # ht_tail = search_for_two_breaks(ht_tail, args.chisq_threshold)
            # logger.info("Writing out last 9230 rows...")
            # ht_tail = ht_tail.checkpoint("gs://gnomad-tmp/kc/simul_breaks_9229.ht", overwrite=args.overwrite)

            # logger.info("Joining and writing...")
            # ht = ht_head.union(ht_tail)
            # ht.write("gs://regional_missense_constraint/temp/simul_split_test.ht", overwrite=args.overwrite)

            # logger.info("Writing out simultaneous breaks HT...")
            # simul_path = "gs://regional_missense_constraint/temp/simul_test.ht"
            # ht = ht.checkpoint(simul_path, overwrite=args.overwrite)

            # Collecting all transcripts with two simultaneous breaks
            # simul_break_transcripts = ht.aggregate(
            #    hl.agg.collect_as_set(ht.transcript),
            # )
            # simul_break_transcripts = hl.literal(simul_break_transcripts)

        # NOTE: This is only necessary for gnomAD v2
        # Fixed expected counts for any genes that span PAR and non-PAR regions
        # after running on gnomAD v2
        if args.fix_xg:
            hl.init(log="/RMC_fix_XG.log")

            logger.info("Reading in exome HT...")
            exome_ht = filtered_exomes.ht()

            logger.info("Reading in context HT...")
            context_ht = processed_context.ht()

            logger.info("Adding models from constraint prep HT...")
            constraint_prep_ht = constraint_prep.ht().select()
            context_ht = context_ht.annotate_globals(
                **constraint_prep_ht.index_globals()
            )

            logger.info("Adding coverage correction to mutation rate probabilities...")
            context_ht = context_ht.annotate(
                raw_mu_snp=context_ht.mu_snp,
                mu_snp=context_ht.mu_snp
                * get_coverage_correction_expr(
                    context_ht.exome_coverage, context_ht.coverage_model
                ),
            )

            logger.info(
                "Fixing XG (gene that spans PAR and non-PAR regions on chrX)..."
            )
            xg = fix_xg(context_ht, exome_ht, args.xg_transcript)

            logger.info("Searching for a break in XG...")
            xg = process_transcripts(xg, chisq_threshold=args.chisq_threshold)

            logger.info("Checking whether there was one break...")
            is_break_ht = xg.filter(xg.is_break)
            if is_break_ht.count() == 0:
                logger.info("XG didn't have one single significant break...")
                transcript_ht = xg.group_by(xg.transcript).aggregate(
                    end_pos=hl.agg.max(xg.locus.position),
                    start_pos=hl.agg.min(xg.locus.position),
                )
                xg = xg.annotate(
                    start_pos=transcript_ht[xg.transcript].start_pos,
                    end_pos=transcript_ht[xg.transcript].end_pos,
                )
            else:
                logger.info("XG has at least one break!")
                is_break_ht = is_break_ht.checkpoint(
                    f"{temp_path}/XG_one_break.ht", overwrite=args.overwrite
                )
                # NOTE: Did not need to check for additional breaks in XG
                # XG did not have a single significant break in gnomAD v2
                # xg = xg.annotate(is_break=is_break_ht[ht.key].is_break)
                # xg = xg.annotate(break_list=[xg.is_break])

        if args.finalize:
            hl.init(log="/RMC_finalize.log")

            logger.info(
                "Getting start and end positions and total size for each transcript..."
            )
            if not file_exists(f"{temp_path}/transcript.ht"):
                raise DataException(
                    "Transcript HT doesn't exist. Please double check and recreate!"
                )

            if args.remove_outlier_transcripts:
                outlier_transcripts = get_outlier_transcripts()

            logger.info("Reading in context HT...")
            # Drop extra annotations from context HT
            context_ht = processed_context.ht().drop(
                "_obs_scan", "_mu_scan", "forward_oe", "values"
            )
            context_ht = context_ht.filter(
                ~outlier_transcripts.contains(context_ht.transcript)
            )

            logger.info("Reading in results HTs...")
            multiple_breaks_ht = multiple_breaks.ht()
            multiple_breaks_ht = multiple_breaks_ht.filter(
                ~outlier_transcripts.contains(multiple_breaks_ht.transcript)
            )
            simul_breaks_ht = simul_break.ht()
            simul_breaks_ht = simul_breaks_ht.filter(
                ~outlier_transcripts.contains(simul_breaks_ht.transcript)
            )

            logger.info("Getting simultaneous breaks transcripts...")
            simul_break_transcripts = simul_breaks_ht.aggregate(
                hl.agg.collect_as_set(simul_breaks_ht.transcript), _localize=False
            )
            rmc_transcripts = simul_break_transcripts

            logger.info("Getting transcript information for breaks HT...")
            global_fields = multiple_breaks_ht.globals
            n_breaks = [
                int(x.split("_")[1]) for x in global_fields if "break" in x
            ].sort()
            rmc_transcripts = []
            for break_num in n_breaks:
                ht = hl.read_table(f"{temp_path}/break_{break_num}.ht")
                ht = ht.filter(ht.is_break)
                rmc_transcripts.append(
                    ht.aggregate(hl.agg.collect_as_set(ht.transcript), _localize=False)
                )

            logger.info("Removing overlapping transcript information...")
            # Extracting the unique set of transcripts for each break number
            # e.g., keeping only transcripts with a single break
            # and not transcripts also that had additional breaks in "break_1_transcripts"
            filtered_transcripts = {}

            logger.info(
                "Cycling through each set of transcripts with break information..."
            )
            for index, transcripts in enumerate(rmc_transcripts):
                # Cycle through the sets of transcripts for every number of breaks larger than current number of breaks
                for t in rmc_transcripts[index + 1 :]:
                    transcripts = transcripts.difference(t)

                # Separate transcripts with a single break from transcripts with multiple breaks
                # This is because transcripts with a single break are annotated into a separate struct
                # in the release HT globals
                if index == 0:
                    one_break_transcripts = transcripts
                else:
                    filtered_transcripts[f"break_{index + 1}_transcripts"] = transcripts

            # Getting total number of transcripts with evidence of rmc
            total_rmc_transcripts = simul_break_transcripts
            for break_num in filtered_transcripts:
                total_rmc_transcripts = total_rmc_transcripts.union(
                    filtered_transcripts[break_num]
                )
            logger.info(
                "Number of transcripts with evidence of RMC: %s",
                hl.eval(hl.len(total_rmc_transcripts)),
            )

            logger.info("Creating breaks HT...")
            breaks_ht = context_ht.filter(
                total_rmc_transcripts.contains(context_ht.transcript)
            )

            logger.info("Adding simultaneous breaks information to breaks HT...")
            breaks_ht = breaks_ht.annotate(
                simul_break_info=hl.struct(
                    is_break=simul_breaks_ht[breaks_ht.key].is_break,
                    window_end=simul_breaks_ht[breaks_ht.key].window_end,
                    post_window_pos=simul_breaks_ht[breaks_ht.key].post_window_pos,
                )
            )
            logger.info("Adding transcript information to breaks HT globals...")
            breaks_ht = breaks_ht.annotate_globals(
                single_break=hl.struct(transcripts=one_break_transcripts),
                multiple_breaks=hl.struct(**filtered_transcripts),
                simul_breaks=hl.struct(transcripts=simul_break_transcripts),
            )

            logger.info("Creating no breaks HT...")
            no_breaks_ht = context_ht.filter(
                ~total_rmc_transcripts.contains(context_ht.transcript)
            )

            if GNOMAD_VER == "2.1.1":
                logger.info("Reading in XG HT (one-off fix in v2.1.1)...")
                xg_ht = hl.read_table(f"{temp_path}/XG.ht").select(
                    "total_mu",
                    "total_exp",
                    "total_obs",
                    "cumulative_exp",
                    "cumulative_obs",
                    "overall_oe",
                )
                no_breaks_ht = no_breaks_ht.annotate(
                    xg_total_mu=xg_ht[no_breaks_ht.key].total_mu,
                    xg_total_exp=xg_ht[no_breaks_ht.key].total_exp,
                    xg_total_obs=xg_ht[no_breaks_ht.key].total_obs,
                    xg_cum_exp=xg_ht[no_breaks_ht.key].cumulative_exp,
                    xg_cum_obs=xg_ht[no_breaks_ht.key].cumulative_obs,
                    xg_oe=xg_ht[no_breaks_ht.key].overall_oe,
                )
                no_breaks_ht = no_breaks_ht.transmute(
                    total_mu=hl.if_else(
                        hl.is_defined(no_breaks_ht.xg_total_mu),
                        no_breaks_ht.xg_total_mu,
                        no_breaks_ht.total_mu,
                    ),
                    total_exp=hl.if_else(
                        hl.is_defined(no_breaks_ht.xg_total_exp),
                        no_breaks_ht.xg_total_exp,
                        no_breaks_ht.total_exp,
                    ),
                    total_obs=hl.if_else(
                        hl.is_defined(no_breaks_ht.xg_total_obs),
                        no_breaks_ht.xg_total_obs,
                        no_breaks_ht.total_obs,
                    ),
                    cumulative_exp=hl.if_else(
                        hl.is_defined(no_breaks_ht.xg_cum_exp),
                        no_breaks_ht.xg_cum_exp,
                        no_breaks_ht.cumulative_exp,
                    ),
                    cumulative_obs=hl.if_else(
                        hl.is_defined(no_breaks_ht.xg_cum_obs),
                        no_breaks_ht.xg_cum_obs,
                        no_breaks_ht.cumulative_obs,
                    ),
                    overall_oe=hl.if_else(
                        hl.is_defined(no_breaks_ht.xg_oe),
                        no_breaks_ht.xg_oe,
                        no_breaks_ht.overall_oe,
                    ),
                )

            # TODO: Add section chisq calculation here
            if (breaks_ht.count() + no_breaks_ht.count()) != context_ht.count():
                raise DataException(
                    "Row counts for breaks HT (one break, multiple breaks, simul breaks) and no breaks HT doesn't match context HT row count!"
                )

            logger.info("Checkpointing HTs...")
            breaks_ht = breaks_ht.checkpoint(f"{temp_path}/breaks.ht", overwrite=True)
            no_breaks_ht = no_breaks_ht.checkpoint(
                f"{temp_path}/no_breaks.ht", overwrite=True
            )

    finally:
        logger.info("Copying hail log to logging bucket...")
        hl.copy_log(LOGGING_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        "This script searches for regional missense constraint in gnomAD"
    )
    parser.add_argument(
        "--trimers", help="Use trimers instead of heptamers", action="store_true"
    )
    parser.add_argument(
        "--exac", help="Use ExAC Table (not gnomAD Table)", action="store_true"
    )
    parser.add_argument(
        "--n-partitions",
        help="Desired number of partitions for output data",
        type=int,
        default=40000,
    )
    parser.add_argument(
        "--high-cov-cutoff",
        help="Coverage threshold for a site to be considered high coverage",
        type=int,
        default=40,
    )
    parser.add_argument(
        "--chisq-threshold",
        help="Chi-square significance threshold. Value should be 10.8 (single break) and 13.8 (two breaks) (values from ExAC RMC code).",
        type=float,
        default=10.8,
    )
    parser.add_argument(
        "--pre-process-data", help="Pre-process data", action="store_true"
    )
    parser.add_argument(
        "--prep-for-constraint",
        help="Prepare tables for constraint calculations",
        action="store_true",
    )
    parser.add_argument(
        "--skip-calc-oe",
        help="Skip observed and expected variant calculations per transcript. Relevant only to gnomAD v2.1.1!",
        action="store_true",
    )
    parser.add_argument(
        "--search-for-first-break",
        help="Initial search for one break in all transcripts",
        action="store_true",
    )
    parser.add_argument(
        "--search-for-additional-breaks",
        help="Search for additional break in transcripts with one significant break",
        action="store_true",
    )
    parser.add_argument(
        "--search-for-simul-breaks",
        help="Search for two simultaneous breaks in transcripts without a single significant break",
        action="store_true",
    )
    simul_breaks = parser.add_argument_group(
        "simul_breaks",
        description="Options specific to running simultaneous breaks search",
    )
    simul_breaks.add_argument(
        "--transcript-tsv",
        help="Path to store transcripts to search for two simultaneous breaks. Path should be to a file in Google cloud storage.",
        default=f"{temp_path}/no_break_transcripts.tsv",
    )
    simul_breaks.add_argument(
        "--get-no-break-transcripts",
        help="Get all transcripts without evidence of one significant break (to search for two simultaneous breaks.",
        action="store_true",
    )
    simul_breaks.add_argument(
        "--get-min-window-size",
        help="Determine smallest possible window size for simultaneous breaks.",
        action="store_true",
    )
    simul_breaks.add_argument(
        "--get-total-exome-bases",
        help="Get total number of bases in the exome. If not set, will pull default value from TOTAL_EXOME_BASES.",
        action="store_true",
    )
    simul_breaks.add_argument(
        "--get-total-gnomad-missense",
        help="Get total number of missense variants in gnomAD. If not set, will pull default value from TOTAL_GNOMAD_MISSENSE.",
        action="store_true",
    )
    simul_breaks.add_argument(
        "--min-num-obs",
        help="Number of observed variants. Used when determining the smallest possible window size for simultaneous breaks.",
        default=10,
        type=int,
    )
    simul_breaks.add_argument(
        "--create-grouped-ht",
        help="Create hail Table grouped by transcript with cumulative observed and expected missense values collected into lists.",
        action="store_true",
    )
    simul_breaks.add_argument(
        "--simul-breaks-temp-path",
        help="Path to bucket with temporary simultaneous breaks results tables.",
        default=f"{temp_path}/simul_breaks_x*.ht",
    )
    parser.add_argument(
        "--fix-xg",
        help="Fix XG (gene that spans PAR and non-PAR regions on chrX). Required only for gnomAD v2",
        action="store_true",
    )
    parser.add_argument(
        "--xg-transcript", help="Transcript ID for XG", default="ENST00000419513",
    )
    parser.add_argument(
        "--finalize",
        help="Combine and reformat (finalize) RMC output",
        action="store_true",
    )
    parser.add_argument(
        "--overwrite-transcript-ht",
        help="Overwrite the transcript HT (HT with start/end positions and transcript sizes), even if it already exists.",
        action="store_true",
    )
    parser.add_argument(
        "--remove-outlier-transcripts",
        help="Remove outlier transcripts (transcripts with too many/few LoF, synonymous, or missense variants)",
        action="store_true",
    )
    parser.add_argument(
        "--overwrite", help="Overwrite existing data", action="store_true"
    )
    parser.add_argument(
        "--slack-channel",
        help="Send message to Slack channel/user",
        default="@kc (she/her)",
    )
    args = parser.parse_args()

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
