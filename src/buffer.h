#ifndef BUFFER_H
#define BUFFER_H

#include "config.h"

#include <linux/types.h>
#include <linux/ktime.h>
#if HRP_HEAP_ALLOCATED_RB && HRP_EXLARGE_HEAP_ALLOCATED_RB
#include <linux/mm_types.h>
#endif

typedef struct {
    u64 kts;
    unsigned long long stall_mem;
    unsigned long long inst_retire;
    unsigned long long stalls_sb;
    unsigned long long cpu_unhalt;
    unsigned long long llc_misses;
    unsigned long long sw_prefetch;
#if HRP_LOG_IMC
    unsigned long long imc_reads;
    unsigned long long imc_writes;
#endif
#if HRP_USE_RDT
    unsigned long long total_bw;
#if HRP_RDT_INCLUDE_LOCAL_BW
    unsigned long long local_bw;
#endif
    unsigned long long occupancy;
#endif
} HrperfTick;

typedef struct __attribute__((__packed__)) {
    int cpu_id;
    HrperfTick tick;
} HrperfLogEntry;

typedef struct {
#if HRP_HEAP_ALLOCATED_RB
    HrperfLogEntry *buffer;
#if HRP_EXLARGE_HEAP_ALLOCATED_RB
    // For extremely large ring buffers, we allocate per-page and vmap them.
    // Track per-instance page array and count for correct teardown.
    struct page **pages;
    size_t num_pages;
#endif
#else
    HrperfLogEntry buffer[HRP_PMC_BUFFER_SIZE];
#endif
    volatile unsigned int head;
    volatile unsigned int tail;
} HrperfRingBuffer;

bool is_full(const HrperfRingBuffer *rb);
int init_ring_buffer(HrperfRingBuffer *rb);
void enqueue(HrperfRingBuffer *rb, HrperfLogEntry data);
void deinit_ring_buffer(HrperfRingBuffer *rb);

#endif // BUFFER_H
