/*
    Mainly Intel architectrual MSR addresses consistent across all Intel CPUs.

    Some are also defined in linux headers, but the macro names are not align
    with Intel manuals. Here the names MSR_XXX are corresponds to the XXX in
    Intel 64 and IA-32 Architectures Software Developer’s Manual Volume 3b
*/

/* control MSRs */
#define MSR_IA32_FIXED_CTR_CTRL	0x0000038d
#define MSR_IA32_GLOBAL_STATUS	0x0000038e
#define MSR_IA32_GLOBAL_CTRL	0x0000038f
#define MSR_IA32_GLOBAL_OVF_CTRL	0x00000390

/* fixed function counters MSRs*/
#define MSR_IA32_FIXED_CTR0	0x00000309 /*INST_RETIRED*/
#define MSR_IA32_FIXED_CTR1	0x0000030a /*CPU_CLK_UNHALTED*/
#define MSR_IA32_FIXED_CTR2	0x0000030b
#define MSR_IA32_FIXED_CTR3	0x0000030c

/* esel + counter MSR pairs */
#define MSR_IA32_PERFEVTSEL0 0x00000186
#define MSR_IA32_PERFEVTSEL1 0x00000187
#define MSR_IA32_PERFEVTSEL2 0x00000188
#define MSR_IA32_PERFEVTSEL3 0x00000189
#define MSR_IA32_PERFEVTSEL4 0x0000018a // available for saphhire machines
#define MSR_IA32_PERFEVTSEL5 0x0000018b 
#define MSR_IA32_PERFEVTSEL6 0x0000018c 
#define MSR_IA32_PERFEVTSEL7 0x0000018d
#define MSR_IA32_PMC0 0x000000c1
#define MSR_IA32_PMC1 0x000000c2
#define MSR_IA32_PMC2 0x000000c3 // available since Arch PMC V3
#define MSR_IA32_PMC3 0x000000c4 
#define MSR_IA32_PMC4 0x000000c5 // available for saphhire machines
#define MSR_IA32_PMC5 0x000000c6 
#define MSR_IA32_PMC6 0x000000c7 
#define MSR_IA32_PMC7 0x000000c8 

/* offcore response events selector MSRs */
#define MSR_OFFCORE_RSP0 0x000001A6
#define MSR_OFFCORE_RSP1 0x000001A7