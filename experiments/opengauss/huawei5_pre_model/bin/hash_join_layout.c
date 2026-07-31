#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef void *Ptr;

typedef struct HashJoinTableLayout {
    int nbuckets;
    int log2_nbuckets;
    Ptr buckets;
    int nbuckets_original;
    int nbuckets_optimal;
    int log2_nbuckets_optimal;
    bool keepNulls;
    bool skewEnabled;
    Ptr skewBucket;
    int skewBucketLen;
    int nSkewBuckets;
    Ptr skewBucketNums;
    int nbatch;
    int curbatch;
    int nbatch_original;
    int nbatch_outstart;
    bool growEnabled;
    double totalTuples;
    double skewTuples;
    Ptr innerBatchFile;
    Ptr outerBatchFile;
    Ptr outer_hashfunctions;
    Ptr inner_hashfunctions;
    Ptr hashStrict;
    int64_t spaceUsed;
    int64_t spaceAllowed;
    int64_t spacePeak;
    int64_t spaceUsedSkew;
    int64_t spaceAllowedSkew;
    Ptr hashCxt;
    Ptr batchCxt;
    Ptr chunks;
    int64_t width[2];
    bool causedBySysRes;
    int64_t maxMem;
    int spreadNum;
    Ptr spill_size;
    uint64_t spill_count;
    Ptr collations;
} HashJoinTableLayout;

#define PRINT_OFFSET(field) printf("#define HJ_OFF_%-24s %zu\n", #field, offsetof(HashJoinTableLayout, field))

int main(void)
{
    printf("#define HJ_LAYOUT_SIZE %zu\n", sizeof(HashJoinTableLayout));
    PRINT_OFFSET(nbuckets);
    PRINT_OFFSET(nbuckets_optimal);
    PRINT_OFFSET(skewEnabled);
    PRINT_OFFSET(skewBucketLen);
    PRINT_OFFSET(nSkewBuckets);
    PRINT_OFFSET(nbatch);
    PRINT_OFFSET(curbatch);
    PRINT_OFFSET(nbatch_original);
    PRINT_OFFSET(totalTuples);
    PRINT_OFFSET(skewTuples);
    PRINT_OFFSET(spaceUsed);
    PRINT_OFFSET(spaceAllowed);
    PRINT_OFFSET(spacePeak);
    PRINT_OFFSET(spaceUsedSkew);
    PRINT_OFFSET(width);
    printf("#define HJ_OFF_width_count              %zu\n", offsetof(HashJoinTableLayout, width[0]));
    printf("#define HJ_OFF_width_sum_or_avg         %zu\n", offsetof(HashJoinTableLayout, width[1]));
    PRINT_OFFSET(causedBySysRes);
    PRINT_OFFSET(maxMem);
    PRINT_OFFSET(spreadNum);
    PRINT_OFFSET(spill_size);
    PRINT_OFFSET(spill_count);
    return 0;
}
