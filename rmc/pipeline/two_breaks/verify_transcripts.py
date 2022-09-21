"""
This script verifies that all transcripts without one significant break were run through the two simultaneous breaks search.

This step should be run locally.
"""
import argparse
import logging

import hail as hl

from gnomad.resources.resource_utils import DataException
from gnomad.utils.slack import slack_notifications

from rmc.resources.basics import TEMP_PATH_WITH_DEL
from rmc.resources.rmc import sections_to_simul_by_threshold_path
from rmc.slack_creds import slack_token
from rmc.utils.simultaneous_breaks import check_for_successful_transcripts


logging.basicConfig(
    format="%(asctime)s (%(name)s %(lineno)s): %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger("verify_transcripts")
logger.setLevel(logging.INFO)


def main(args):
    """Verify that all transcripts were run through the two simultaneous breaks search."""
    hl.init(
        log="search_for_two_breaks_verify_transcripts.log", tmp_dir=TEMP_PATH_WITH_DEL
    )

    logger.info("Verifying that all transcripts were processed...")
    transcripts = list(
        hl.eval(
            hl.experimental.read_expression(
                sections_to_simul_by_threshold_path(
                    is_rescue=args.is_rescue,
                    search_num=args.search_num,
                    is_over_threshold=False,
                )
            ).union(
                hl.experimental.read_expression(
                    sections_to_simul_by_threshold_path(
                        is_rescue=args.is_rescue,
                        search_num=args.search_num,
                        is_over_threshold=True,
                    )
                )
            )
        )
    )
    missing_transcripts = check_for_successful_transcripts(
        transcripts=transcripts,
        is_rescue=args.is_rescue,
        search_num=args.search_num,
    )
    if len(missing_transcripts) > 0:
        logger.error(missing_transcripts)
        raise DataException(f"{len(missing_transcripts)} are missing! Please rerun.")

    # Check if TTN was run and print a warning if it wasn't
    # TTN ID isn't included in `sections_to_simul_by_threshold_path`
    # It needs to be run separately due to its size
    logger.info("Checking if TTN was processed...")
    ttn_missing = check_for_successful_transcripts(
        transcripts=[args.ttn],
        is_rescue=args.is_rescue,
        search_num=args.search_num,
    )
    if len(ttn_missing) > 0:
        logger.warning(
            "TTN wasn't processed successfully. Double check whether this is expected!"
        )
    logger.info("Done searching for transcript success TSVS!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="""
        This regional missense constraint script searches for two simultaneous breaks in transcripts
        without evidence of a single significant break.
        """,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--chisq-threshold",
        help="Chi-square significance threshold. Value should be 9.2 (value adjusted from ExAC code due to discussion with Mark).",
        type=float,
        default=9.2,
    )
    parser.add_argument(
        "--search-num",
        help="Search iteration number (e.g., second round of searching for two simultaneous breaks would be 2).",
        type=int,
    )
    parser.add_argument(
        "--is-rescue",
        help="""
        Whether search is part of the 'rescue' pathway (pathway
        with lower chi square significance cutoff).
        """,
        action="store_true",
    )
    parser.add_argument(
        "--slack-channel",
        help="Send message to Slack channel/user.",
        default="@kc (she/her)",
    )
    parser.add_argument(
        "--ttn",
        help="TTN transcript ID. TTN is so large that it needs to be treated separately.",
        default="ENST00000589042",
    )

    args = parser.parse_args()

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
