#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>

typedef void *Ptr;

typedef struct TupleHashTableLayout {
    Ptr hashtab;
    int numCols;
    Ptr keyColIdx;
    Ptr tab_hash_funcs;
    Ptr tab_eq_funcs;
    Ptr tablecxt;
    Ptr tempcxt;
    size_t entrysize;
    Ptr tableslot;
    Ptr inputslot;
    Ptr in_hash_funcs;
    Ptr cur_eq_funcs;
    int64_t width;
    bool add_width;
    bool causedBySysRes;
    Ptr tab_collations;
} TupleHashTableLayout;

typedef struct AggWriteFileControlLayout {
    bool spillToDisk;
    bool finishwrite;
    int strategy;
    int runState;
    int64_t useMem;
    int64_t totalMem;
    int64_t inmemoryRownum;
    Ptr m_hashAggSource;
    Ptr filesource;
    int filenum;
    int curfile;
    int64_t maxMem;
    int spreadNum;
} AggWriteFileControlLayout;

#define PRINT(prefix, type, field) printf("#define %s_%-22s %zu\n", prefix, #field, offsetof(type, field))

int main(void)
{
    PRINT("TH", TupleHashTableLayout, tablecxt);
    PRINT("TH", TupleHashTableLayout, entrysize);
    PRINT("TH", TupleHashTableLayout, width);
    PRINT("TH", TupleHashTableLayout, add_width);
    PRINT("TH", TupleHashTableLayout, causedBySysRes);
    PRINT("AFC", AggWriteFileControlLayout, spillToDisk);
    PRINT("AFC", AggWriteFileControlLayout, totalMem);
    PRINT("AFC", AggWriteFileControlLayout, inmemoryRownum);
    PRINT("AFC", AggWriteFileControlLayout, filenum);
    PRINT("AFC", AggWriteFileControlLayout, maxMem);
    PRINT("AFC", AggWriteFileControlLayout, spreadNum);
    return 0;
}
