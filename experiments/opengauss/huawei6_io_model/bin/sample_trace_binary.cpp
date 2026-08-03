#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <zlib.h>

#pragma pack(push, 1)
struct SampleEvent {
    uint64_t page_id;
    uint64_t ts_ns;
    uint64_t strategy_ptr;
    uint32_t tid;
    int8_t strategy_type;
    uint8_t hit;
    uint16_t reserved;
};
#pragma pack(pop)

static uint64_t mix64(uint64_t value) {
    value ^= value >> 33;
    value *= 0xff51afd7ed558ccdULL;
    value ^= value >> 33;
    value *= 0xc4ceb9fe1a85ec53ULL;
    value ^= value >> 33;
    return value;
}

static bool next_uint(char*& cursor, uint64_t& value) {
    if (*cursor == ',') {
        ++cursor;
    }
    char* end = nullptr;
    value = std::strtoull(cursor, &end, 10);
    if (end == cursor) {
        return false;
    }
    cursor = end;
    return true;
}

int main(int argc, char** argv) {
    if (argc != 5) {
        std::fprintf(stderr, "usage: %s TRACE.gz OUTPUT.bin SAMPLE_EVERY END_NS\n", argv[0]);
        return 2;
    }
    const int sample_every = std::max(1, std::atoi(argv[3]));
    const uint64_t end_ns = std::strtoull(argv[4], nullptr, 10);
    gzFile input = gzopen(argv[1], "rb");
    if (!input) {
        std::perror("gzopen");
        return 1;
    }
    FILE* output = std::fopen(argv[2], "wb");
    if (!output) {
        std::perror("fopen");
        gzclose(input);
        return 1;
    }

    char line[512];
    uint64_t seen = 0;
    uint64_t kept = 0;
    while (gzgets(input, line, sizeof(line)) != nullptr) {
        if (line[0] != 'S' || line[1] != 'B' || line[2] != ',') {
            continue;
        }
        char* cursor = line + 2;
        uint64_t tid = 0, relation = 0, block = 0, ts = 0, hit = 0, meta = 0;
        if (!next_uint(cursor, tid) || !next_uint(cursor, relation) ||
            !next_uint(cursor, block) || !next_uint(cursor, ts) ||
            !next_uint(cursor, hit) || !next_uint(cursor, meta)) {
            continue;
        }
        if (ts > end_ns) {
            break;
        }
        ++seen;
        const uint64_t page_id = (relation << 32) | (block & 0xffffffffULL);
        if (sample_every > 1 && mix64(page_id) % sample_every != 0) {
            continue;
        }
        SampleEvent event{};
        event.page_id = page_id;
        event.ts_ns = ts;
        event.tid = static_cast<uint32_t>(tid);
        event.hit = hit != 0;
        event.strategy_type = -1;
        if (meta > 1000000000ULL) {
            event.strategy_ptr = meta >> 4;
            event.strategy_type = static_cast<int8_t>((meta & 0xfULL) - 1);
        } else if (meta > 0) {
            event.strategy_type = static_cast<int8_t>(meta / 1000000ULL - 1);
        }
        if (std::fwrite(&event, sizeof(event), 1, output) != 1) {
            std::perror("fwrite");
            std::fclose(output);
            gzclose(input);
            return 1;
        }
        ++kept;
    }
    std::fclose(output);
    gzclose(input);
    std::fprintf(stderr, "seen=%llu kept=%llu event_size=%zu\n",
                 static_cast<unsigned long long>(seen),
                 static_cast<unsigned long long>(kept),
                 sizeof(SampleEvent));
    return 0;
}
