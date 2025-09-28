#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <pthread.h>
#include <unistd.h>
#include <sched.h>
#include <time.h>
#include "hrperf_control.h"

// Macros to configure parameters
#define MEMORY_SIZE (4UL * 1024 * 1024 * 1024) // 4GB
#define CORE_ID 2

void pin_to_core(int core_id) {
    cpu_set_t cpuset;
    CPU_ZERO(&cpuset);
    CPU_SET(core_id, &cpuset);
    pthread_t current_thread = pthread_self();
    pthread_setaffinity_np(current_thread, sizeof(cpu_set_t), &cpuset);
}

void* memory_scanner(void* arg) {
    uint8_t *memory_region = (uint8_t*) arg;
    struct timespec start_time, end_time;
    uint64_t start_ns, end_ns, elapsed_ns;

    // Get start time using CLOCK_MONOTONIC_RAW
    clock_gettime(CLOCK_MONOTONIC_RAW, &start_time);
    start_ns = (uint64_t)start_time.tv_sec * 1000000000ULL + start_time.tv_nsec;

    printf("Scan start time: %lu.%09lu seconds (CLOCK_MONOTONIC_RAW)\n",
           (unsigned long)start_time.tv_sec, start_time.tv_nsec);

    // Start profiling
    hrperf_start();

    // Perform one full scan
    for (size_t i = 0; i < MEMORY_SIZE; i++) {
        uint8_t value = memory_region[i];
        (void)value; // Prevent unused variable warning
    }

    // Pause profiling
    hrperf_pause();

    // Get end time using CLOCK_MONOTONIC_RAW
    clock_gettime(CLOCK_MONOTONIC_RAW, &end_time);
    end_ns = (uint64_t)end_time.tv_sec * 1000000000ULL + end_time.tv_nsec;
    elapsed_ns = end_ns - start_ns;

    printf("Scan end time: %lu.%09lu seconds (CLOCK_MONOTONIC_RAW)\n",
           (unsigned long)end_time.tv_sec, end_time.tv_nsec);
    printf("Total scan time: %llu.%09llu seconds (%lu nanoseconds)\n",
           elapsed_ns / 1000000000ULL, elapsed_ns % 1000000000ULL, elapsed_ns);

    // Instead of running forever, return after one scan
    return NULL;
}

int main() {
    // Pin this program to core 1
    pin_to_core(CORE_ID);

    // Allocate memory region
    uint8_t *memory_region = (uint8_t*) malloc(MEMORY_SIZE);
    if (!memory_region) {
        perror("Failed to allocate memory");
        return 1;
    }

    // Initialize memory to ensure it's actually allocated
    memset(memory_region, 0, MEMORY_SIZE);

    // Create a thread to scan the memory region
    pthread_t scanner_thread;
    pthread_create(&scanner_thread, NULL, memory_scanner, memory_region);

    // Join the scanner thread
    pthread_join(scanner_thread, NULL);

    // Free allocated memory
    free(memory_region);

    return 0;
}