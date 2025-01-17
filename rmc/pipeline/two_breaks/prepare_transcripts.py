"""
This script prepares the inputs to the two simultaneous breaks search.

This script has two possible steps:
- Group not one single break found Table by transcript/transcript section.
- Split transcripts/transcript sections in this Table based on number of possible missense variants.

Both steps should be run in Dataproc.
"""
import argparse
import logging

import hail as hl

from gnomad.utils.slack import slack_notifications

from rmc.resources.basics import LOGGING_PATH, TEMP_PATH_WITH_FAST_DEL
from rmc.resources.rmc import (
    grouped_single_no_break_ht_path,
    single_search_round_ht_path,
)
from rmc.slack_creds import slack_token
from rmc.utils.simultaneous_breaks import (
    group_no_single_break_found_ht,
    split_sections_by_len,
)


logging.basicConfig(
    format="%(asctime)s (%(name)s %(lineno)s): %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger("prepare_transcripts")
logger.setLevel(logging.INFO)


def main(args):
    """Prepare input Table for two simultaneous breaks search."""
    try:
        grouped_ht_path = grouped_single_no_break_ht_path(
            args.search_num,
        )

        if args.command == "create-grouped-ht":
            hl.init(
                log=f"/round{args.search_num}_search_for_two_breaks_create_grouped_ht.log",
                tmp_dir=TEMP_PATH_WITH_FAST_DEL,
            )

            logger.info(
                "Creating grouped HT with lists of cumulative observed and expected missense values..."
            )
            group_no_single_break_found_ht(
                ht_path=single_search_round_ht_path(
                    search_num=args.search_num,
                    is_break_found=False,
                    is_breakpoint_only=False,
                ),
                out_ht_path=grouped_ht_path,
                group_str="section",
                overwrite=args.overwrite,
            )

        if args.command == "split-sections":
            hl.init(
                log=f"/round{args.search_num}_search_for_two_breaks_split_sections.log",
                tmp_dir=TEMP_PATH_WITH_FAST_DEL,
            )
            split_sections_by_len(
                ht_path=grouped_ht_path,
                group_str="section",
                search_num=args.search_num,
                missense_len_threshold=args.missense_len_threshold,
                overwrite=args.overwrite,
            )

    finally:
        logger.info("Copying hail log to logging bucket...")
        hl.copy_log(LOGGING_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="This regional missense constraint script prepares the input Table for the two simultaneous breaks search.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--overwrite", help="Overwrite existing data.", action="store_true"
    )
    parser.add_argument(
        "--slack-channel",
        help="Send message to Slack channel/user.",
    )
    parser.add_argument(
        "--search-num",
        help="Search iteration number (e.g., second round of searching for two simultaneous breaks would be 2).",
        type=int,
    )
    # Create subparsers for each step
    # Need to specify `dest` to be able to check which subparser is being invoked
    # `dest`: https://docs.python.org/3/library/argparse.html#dest
    subparsers = parser.add_subparsers(title="command", dest="command", required=True)

    create_grouped_ht = subparsers.add_parser(
        "create-grouped-ht",
        help="""
        Create hail Table grouped by transcript/transcript section with cumulative observed and expected missense values collected into lists.
        This step should be run in Dataproc.
        """,
    )

    # TODO: Switch from using "missense variants" to "missense sites" where applicable
    split_sections = subparsers.add_parser(
        "split-sections",
        help="""
        Split transcripts/transcript sections based on number of possible missense sites.
        This is used to create batches to run through search for two breaks code.
        This step should be run in Dataproc.
        """,
    )
    split_sections.add_argument(
        "--missense-len-threshold",
        help="Cutoff for number of possible missense sites in transcript/transcript section. Used to create batches.",
        type=int,
        default=5000,
    )
    args = parser.parse_args()

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
