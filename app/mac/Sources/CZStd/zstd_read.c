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
    if (plain == ZSTD_CONTENTSIZE_UNKNOWN || plain == ZSTD_CONTENTSIZE_ERROR) goto fail;
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
