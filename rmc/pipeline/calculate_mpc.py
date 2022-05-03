"""
This script calculates the MPC score.

MPC (missense badness, PolyPhen-2, and regional missense constraint) is a composite score
that predicts the deleteriousness of any given missense variant.
"""
import argparse
import logging

import hail as hl

from gnomad.utils.slack import slack_notifications

from rmc.resources.basics import LOGGING_PATH
from rmc.slack_creds import slack_token
from rmc.utils.mpc import prepare_pop_path_ht


logging.basicConfig(
    format="%(asctime)s (%(name)s %(lineno)s): %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger("calculate_mpc")
logger.setLevel(logging.INFO)


def main(args):
    """Calculate missense badness."""
    try:
        if args.command == "prepare-ht":
            hl.init(log="/write_pop_path_ht.log")
            prepare_pop_path_ht()

    finally:
        logger.info("Copying hail log to logging bucket...")
        hl.copy_log(LOGGING_PATH)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        "This regional missense constraint script calculates the MPC (missense badness, PolyPhen-2, and regional missense constraint) score."
    )
    parser.add_argument(
        "--overwrite", help="Overwrite existing data.", action="store_true"
    )
    parser.add_argument(
        "--slack-channel",
        help="Send message to Slack channel/user.",
    )

    # Create subparsers for each step
    # Need to specify `dest` to be able to check which subparser is being invoked
    # `dest`: https://docs.python.org/3/library/argparse.html#dest
    subparsers = parser.add_subparsers(title="command", dest="command", required=True)

    prepare_ht = subparsers.add_parser(
        "prepare-ht",
        help="""
        Prepare Table with 'population' (common missense variants in gnomAD) and 'pathogenic'
        (ClinVar pathogenic/likely pathogenic missense variants in severe haploinsufficient genes) variants.
        This step joins gnomAD and ClinVar variants, annotates them with PolyPhen-2, missense badness,
        CADD (raw and phred), BLOSUM, Grantham, and missense observed/expected (OE) raio, and removes
        any variants with undefined annotations.
        """,
    )

    args = parser.parse_args()

    if args.slack_channel:
        with slack_notifications(slack_token, args.slack_channel):
            main(args)
    else:
        main(args)
