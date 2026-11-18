/* Decode one transcript copy. The body is Meta's zstd v1.5.7 single-file
 * decoder (zstddeclib.inc), BSD or GPLv2, either at the user's option.
 * Generated from facebook/zstd tag v1.5.7; not a hand edit. */
#include "zstd_read.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define ZSTD_STATIC_LINKING_ONLY
#include "zstddeclib.inc"

static const unsigned char kMagic[4] = {0x28, 0xb5, 0x2f, 0xfd};

/* A frame written through stdin has no content size. Grow the output
 * until the stream says the frame is done. */
static int decode_stream(const uint8_t *in, size_t in_size,
                         uint8_t **decoded, size_t *got) {
    ZSTD_DCtx *ctx = ZSTD_createDCtx();
    ZSTD_inBuffer input = {in, in_size, 0};
    size_t cap = ZSTD_DStreamOutSize();
    uint8_t *out;
    size_t used = 0;
    if (ctx == NULL) return 1;
    if (cap < 1) cap = 1 << 16;
    out = (uint8_t *)malloc(cap + 1);
    if (out == NULL) {
        ZSTD_freeDCtx(ctx);
        return 1;
    }
    for (;;) {
        ZSTD_outBuffer output;
        size_t rc;
        if (used == cap) {
            size_t next = cap * 2;
            uint8_t *grown = (uint8_t *)realloc(out, next + 1);
            if (grown == NULL) {
                free(out);
                ZSTD_freeDCtx(ctx);
                return 1;
            }
            out = grown;
            cap = next;
        }
        output.dst = out + used;
        output.size = cap - used;
        output.pos = 0;
        rc = ZSTD_decompressStream(ctx, &output, &input);
        used += output.pos;
        if (ZSTD_isError(rc)) {
            free(out);
            ZSTD_freeDCtx(ctx);
            return 1;
        }
        if (rc == 0) {
            ZSTD_freeDCtx(ctx);
            *decoded = out;
            *got = used;
            return 0;
        }
    }
}

long long swe_zstd_content_size(const char *path) {
    FILE *file;
    unsigned char head[ZSTD_FRAMEHEADERSIZE_MAX];
    size_t got;
    unsigned long long plain;
    if (path == NULL) return -1;
    file = fopen(path, "rb");
    if (file == NULL) return -1;
    got = fread(head, 1, sizeof head, file);
    fclose(file);
    if (got < 4 || memcmp(head, kMagic, 4) != 0) return -1;
    plain = ZSTD_getFrameContentSize(head, got);
    if (plain == ZSTD_CONTENTSIZE_UNKNOWN || plain == ZSTD_CONTENTSIZE_ERROR) return -1;
    return (long long)plain;
}

int swe_zstd_read_file(const char *path, uint8_t **out, size_t *out_size) {
    FILE *file;
    uint8_t *in = NULL;
    uint8_t *decoded = NULL;
    long length;
    unsigned long long plain;
    size_t got;
    if (path == NULL || out == NULL || out_size == NULL) return 1;
    file = fopen(path, "rb");
    if (file == NULL) return 1;
    if (fseek(file, 0, SEEK_END) != 0) goto fail;
    length = ftell(file);
    if (length < 0 || fseek(file, 0, SEEK_SET) != 0) goto fail;
    in = (uint8_t *)malloc((size_t)length + 1);
    if (in == NULL) goto fail;
    if (length > 0 && fread(in, 1, (size_t)length, file) != (size_t)length) goto fail;
    fclose(file);
    file = NULL;
    if ((size_t)length < 4 || memcmp(in, kMagic, 4) != 0) {
        *out = in;
        *out_size = (size_t)length;
        return 0;
    }
    plain = ZSTD_getFrameContentSize(in, (size_t)length);
    if (plain == ZSTD_CONTENTSIZE_ERROR) goto fail;
    /* The mirror writes a frame through the zstd command's stdin, which
     * omits the decoded length. Stream it, growing the buffer. */
    if (plain == ZSTD_CONTENTSIZE_UNKNOWN) {
        if (decode_stream(in, (size_t)length, &decoded, &got) != 0) goto fail;
        free(in);
        *out = decoded;
        *out_size = got;
        return 0;
    }
    decoded = (uint8_t *)malloc((size_t)plain + 1);
    if (decoded == NULL) goto fail;
    got = ZSTD_decompress(decoded, (size_t)plain, in, (size_t)length);
    free(in);
    if (ZSTD_isError(got) || got != (size_t)plain) {
        free(decoded);
        return 1;
    }
    *out = decoded;
    *out_size = got;
    return 0;
fail:
    if (file != NULL) fclose(file);
    free(in);
    return 1;
}
