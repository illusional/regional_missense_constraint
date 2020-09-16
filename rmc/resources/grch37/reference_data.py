import hail as hl

from gnomad.resources.resource_utils import (
    import_gencode,
    TableResource,
    VersionedTableResource,
)
from rmc.resources.resource_utils import RESOURCE_PREFIX, FLAGSHIP_LOF


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
    default_version="20190430",
    versions={
        "20190430": TableResource(
            path=f"{RESOURCE_PREFIX}/GRCh37/reference_data/ht/context_fasta_snps_only_vep_20190430.ht",
        )
    },
)

gencode = VersionedTableResource(
    default_version="v19",
    versions={
        "v19": TableResource(
            path=f"{RESOURCE_PREFIX}/GRCh37/reference_data/ht/gencode.v19.annotation.ht",
            import_func=import_gencode,
            import_args={
                "path": f"{RESOURCE_PREFIX}/gencode.v19.annotation.gtf",
                "reference_genome": "GRCh37",
                "skip_invalid_contigs": True,
                "min_partitions": 500,
            },
        )
    },
)

processed_gencode = VersionedTableResource(
    default_version="v19",
    versions={
        "19": TableResource(
            path=f"{RESOURCE_PREFIX}/GRCh37/reference_data/ht/gencode.v19.exons.ht",
        )
    },
)
