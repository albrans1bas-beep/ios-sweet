/*
 * SWEET guest-side MobileGestalt probe.
 *
 * The iOS root image has no /bin/sh, no launchctl and no plutil, so this is a
 * tiny standalone Mach-O built with the iphoneos SDK and launched directly by
 * a repurposed launchd boot job. It records the gestalt SystemGroup container,
 * the on-disk cache plist, and the processes that are alive while SpringBoard
 * sits in its ~19 minute libMobileGestalt init block. The job is in the
 * user/501 domain, so output goes to mobile's Caches; CI copies it out of the
 * image after the smoke run (`--- mgprobe ---`).
 */
#include <sys/types.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include <dlfcn.h>
#include <CoreFoundation/CoreFoundation.h>

/* The hijacked job runs in the user/501 domain as mobile, not root: /private/var/log is root-owned. */
#define OUT "/private/var/mobile/Library/Caches/mgprobe.txt"
#define GESTDIR "/private/var/containers/Shared/SystemGroup/systemgroup.com.apple.mobilegestaltcache"
#define GESTPLIST GESTDIR "/Library/Caches/com.apple.MobileGestalt.plist"

static FILE *out;

static void line(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vfprintf(out, fmt, ap);
	va_end(ap);
	fputc('\n', out);
	fflush(out);
	/* QEMU is SIGKILLed at the end of the smoke: without fsync the lines are
	 * still dirty in the page cache and the image only shows the first one. */
	fsync(fileno(out));
}

static void dump_tree(const char *path, int depth)
{
	struct stat st;
	char child[PATH_MAX];
	DIR *d;
	struct dirent *e;

	if (depth < 0)
		return;
	if (lstat(path, &st) == -1) {
		line("  ! lstat %s: %s", path, strerror(errno));
		return;
	}
	line("  %c %04o %u %llu %s",
	    S_ISDIR(st.st_mode) ? 'd' : S_ISLNK(st.st_mode) ? 'l' : '-',
	    st.st_mode & 07777, st.st_uid,
	    (unsigned long long)st.st_size, path);
	if (!S_ISDIR(st.st_mode))
		return;
	d = opendir(path);
	if (d == NULL) {
		line("  ! opendir %s: %s", path, strerror(errno));
		return;
	}
	while ((e = readdir(d)) != NULL) {
		if (strcmp(e->d_name, ".") == 0 || strcmp(e->d_name, "..") == 0)
			continue;
		snprintf(child, sizeof(child), "%s/%s", path, e->d_name);
		dump_tree(child, depth - 1);
	}
	closedir(d);
}

static void dump_file(const char *path)
{
	unsigned char buf[4096];
	size_t n, total = 0;
	FILE *f = fopen(path, "rb");

	if (f == NULL) {
		line("  MISSING %s (%s)", path, strerror(errno));
		return;
	}
	line("  FILE %s", path);
	while (total < 65536 && (n = fread(buf, 1, sizeof(buf), f)) > 0) {
		fwrite(buf, 1, n, out);
		total += n;
	}
	fputc('\n', out);
	fclose(f);
}

static int want_proc(const char *name)
{
	return strcasestr(name, "gestalt") != NULL ||
	       strcasestr(name, "containermanagerd") != NULL ||
	       strcasestr(name, "SpringBoard") != NULL;
}

/* One MGCopyAnswer call per iteration: the line before the call is fsynced,
 * so if the guest hangs on a key the log names it exactly.  Keys are the
 * ones IconServices/-[ISDefaults init] and SpringBoard's first launch are
 * known to ask for, plus basic ones that must come from the kernel. */
static const char *mg_keys[] = {
	"ProductType",
	"HardwareModel",
	"MainScreenScale",
	"MainScreenWidth",
	"MainScreenHeight",
	"ArtworkDevice1ProductDescription",
	"ArtworkDevice2ProductDescription",
	"DeviceColor",
	"UserAssignedDeviceName",
	"ModelNumber",
};

static void try_mg(int i)
{
	static CFStringRef (*mg)(CFStringRef, Boolean *);
	static int tried;
	const char *key = mg_keys[i % (sizeof(mg_keys) / sizeof(mg_keys[0]))];
	CFStringRef val;
	char buf[256];

	if (!tried) {
		void *h = dlopen("/usr/lib/libMobileGestalt.dylib", RTLD_NOW);
		tried = 1;
		if (h == NULL) {
			line("  dlopen libMobileGestalt: %s", dlerror());
			return;
		}
		mg = (void (*)(CFStringRef, Boolean *))dlsym(h, "MGCopyAnswer");
		line("  dlopen ok, MGCopyAnswer=%p", (void *)mg);
		if (mg == NULL)
			return;
	}
	if (mg == NULL)
		return;
	line("  MGCopyAnswer(%s) calling...", key);
	CFStringRef k = CFStringCreateWithCString(NULL, key, kCFStringEncodingUTF8);
	val = mg(k, NULL);
	if (val == NULL) {
		line("  MGCopyAnswer(%s) -> NULL", key);
	} else if (CFStringGetCString(val, buf, sizeof(buf), kCFStringEncodingUTF8)) {
		line("  MGCopyAnswer(%s) -> %s", key, buf);
	} else {
		line("  MGCopyAnswer(%s) -> <non-string %p>", key, (void *)val);
	}
	CFRelease(k);
}

static void list_procs(void)
{
	int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_ALL, 0 };
	struct kinfo_proc *p;
	size_t size = 0;
	size_t i, count;

	if (sysctl(mib, 4, NULL, &size, NULL, 0) != 0 || size == 0) {
		line("  ! sysctl KERN_PROC_ALL: %s", strerror(errno));
		return;
	}
	p = malloc(size);
	if (p == NULL)
		return;
	if (sysctl(mib, 4, p, &size, NULL, 0) != 0) {
		line("  ! sysctl KERN_PROC_ALL read: %s", strerror(errno));
		free(p);
		return;
	}
	count = size / sizeof(*p);
	for (i = 0; i < count; i++) {
		const char *name = p[i].kp_proc.p_comm;
		if (want_proc(name))
			line("  pid %d %s state %d", (int)p[i].kp_proc.p_pid,
			     name, (int)p[i].kp_proc.p_stat);
	}
	free(p);
}

int main(void)
{
	char stamp[64];
	time_t now;
	int i;

	out = fopen(OUT, "w");
	if (out == NULL) {
		fprintf(stderr, "mgprobe: cannot open %s: %s\n", OUT, strerror(errno));
		return 1;
	}
	line("mgprobe start (compiled helper)");
	sleep(240);
	for (i = 0; i < 10; i++) {
		now = time(NULL);
		strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", gmtime(&now));
		line("--- iteration %d %s", i, stamp);

		line("$ SystemGroup:");
		dump_tree("/private/var/containers/Shared/SystemGroup", 3);
		line("$ gestalt container:");
		dump_tree(GESTDIR, 4);
		line("$ gestalt cache:");
		dump_file(GESTPLIST);
		line("$ interesting processes:");
		list_procs();
		line("$ MGCopyAnswer test %d:", i);
		try_mg(i);
		line("$ client gestalt plist:");
		dump_file("/private/var/preferences/com.apple.MobileGestalt.plist");
		line("$ Bundle dirs:");
		dump_tree("/private/var/containers/Bundle/Application", 2);
		line("");
		fflush(out);
		sleep(60);
	}
	line("mgprobe done");
	fclose(out);
	return 0;
}
