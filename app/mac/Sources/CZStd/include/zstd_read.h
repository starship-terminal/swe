#ifndef SWE_ZSTD_READ_H
#define SWE_ZSTD_READ_H

#include <stddef.h>
#include <stdint.h>

/* 0 on success. *out is malloc'd; the caller frees it.
 * A file that is not a zstd frame is copied through unchanged. */
int swe_zstd_read_file(const char *path, uint8_t **out, size_t *out_size);

#endif
