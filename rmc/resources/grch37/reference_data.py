import hail as hl

from gnomad.resources.resource_utils import (
    TableResource,
    VersionedTableResource,
)
from rmc.resources.resource_utils import FLAGSHIP_LOF, import_gencode, RESOURCE_PREFIX


## Reference genome related resources
full_context = VersionedTableResource(
    default_version="20181129",
    versions={
        "20181129": TableResource(
            path=f"{FLAGSHIP_LOF}/context/Homo_sapiens_assembly19.fasta.snps_only.vep_20181129.ht",
        )
        # NOTE: no import_func necessary for this (will not need to update until we switch to MANE transcripts)
    },
)

processed_context = VersionedTableResource(
    default_version="v1",
    versions={
        "v1": TableResource(
            path=f"{RESOURCE_PREFIX}/GRCh37/reference_data/ht/context_fasta_snps_only_vep_v1.ht",
        )
    },
)
