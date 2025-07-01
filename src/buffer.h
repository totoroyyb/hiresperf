#ifndef BUFFER_H
#define BUFFER_H

#include <linux/types.h>
#include <linux/ktime.h>

#include "config.h"

typedef struct {
    u64 kts;
    unsigned long long stall_mem;
    unsigned long long inst_retire;
    unsigned long long cpu_unhalt;
    unsigned long long llc_misses;
    unsigned long long sw_prefetch;
#ifdef HRP_USE_OFFCORE
    unsigned long long write_estimate;
#endif
#ifdef HRP_LOG_IMC
    unsigned long long imc_reads;
    unsigned long long imc_writes;
#endif
} HrperfTick;

typedef struct __attribute__((__packed__)) {
    int cpu_id;
    HrperfTick tick;
} HrperfLogEntry;

typedef struct {
#ifdef HRP_HEAP_ALLOCATED_RB
    HrperfLogEntry *buffer;
#else
    HrperfLogEntry buffer[HRP_PMC_BUFFER_SIZE];
#endif
    volatile unsigned int head;
    volatile unsigned int tail;
} HrperfRingBuffer;

bool is_full(const HrperfRingBuffer *rb);
int init_ring_buffer(HrperfRingBuffer *rb);
void enqueue(HrperfRingBuffer *rb, HrperfLogEntry data);

#endif // BUFFER_H
