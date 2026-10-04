/*
 * Microbenchmarks of basic kernel paths. Prints METRIC lines for lab.
 *   getpid    - bare syscall entry/exit cost
 *   read      - 1-byte read from /dev/zero (VFS + char driver)
 *   pipe_rtt  - pipe ping-pong between two processes (wakeup + context switch)
 *   fork      - fork + child _exit + waitpid
 *   pagefault - first touch of a freshly mmap'd anonymous page
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static double now_ns(void)
{
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec * 1e9 + ts.tv_nsec;
}

static void metric(const char *name, double v, const char *unit)
{
	printf("METRIC %s %.3f %s lower\n", name, v, unit);
}

static void bench_getpid(void)
{
	const long n = 5000000;
	double t = now_ns();
	for (long i = 0; i < n; i++)
		syscall(SYS_getpid);	/* glibc does not cache this one */
	metric("getpid", (now_ns() - t) / n, "ns");
}

static void bench_read(void)
{
	const long n = 2000000;
	char c;
	int fd = open("/dev/zero", O_RDONLY);
	double t = now_ns();
	for (long i = 0; i < n; i++)
		if (read(fd, &c, 1) != 1)
			exit(1);
	metric("read_devzero", (now_ns() - t) / n, "ns");
	close(fd);
}

static void bench_pipe(void)
{
	const long n = 200000;
	int ping[2], pong[2];
	char c = 0;

	if (pipe(ping) || pipe(pong))
		exit(1);
	if (fork() == 0) {
		for (long i = 0; i < n; i++)
			if (read(ping[0], &c, 1) != 1 || write(pong[1], &c, 1) != 1)
				_exit(1);
		_exit(0);
	}
	double t = now_ns();
	for (long i = 0; i < n; i++)
		if (write(ping[1], &c, 1) != 1 || read(pong[0], &c, 1) != 1)
			exit(1);
	metric("pipe_rtt", (now_ns() - t) / n / 1000, "us");
	wait(NULL);
}

static void bench_fork(void)
{
	const long n = 5000;
	double t = now_ns();
	for (long i = 0; i < n; i++) {
		pid_t p = fork();
		if (p == 0)
			_exit(0);
		waitpid(p, NULL, 0);
	}
	metric("fork_exit_wait", (now_ns() - t) / n / 1000, "us");
}

static void bench_pagefault(void)
{
	const long pages = 65536;	/* 256 MiB */
	long psz = sysconf(_SC_PAGESIZE);
	char *p = mmap(NULL, pages * psz, PROT_READ | PROT_WRITE,
		       MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED)
		exit(1);
	madvise(p, pages * psz, MADV_NOHUGEPAGE);
	double t = now_ns();
	for (long i = 0; i < pages; i++)
		p[i * psz] = 1;
	metric("pagefault", (now_ns() - t) / pages, "ns");
	munmap(p, pages * psz);
}

int main(void)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	bench_getpid();
	bench_read();
	bench_pipe();
	bench_fork();
	bench_pagefault();
	return 0;
}
