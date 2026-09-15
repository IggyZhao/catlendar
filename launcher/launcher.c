/* Catlendar launcher.
   Stays alive as the app process and runs the Python agent as its child, so
   macOS attributes Accessibility and Calendar prompts to Catlendar.app and
   reads the usage strings from this bundle's Info.plist. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <spawn.h>
#include <libgen.h>
#include <limits.h>
#include <sys/wait.h>
#include <mach-o/dyld.h>

extern char **environ;
static pid_t child_pid = 0;

static void forward_signal(int sig) {
    if (child_pid > 0) kill(child_pid, sig);
}

int main(int argc, char **argv) {
    char exe[PATH_MAX];
    uint32_t size = sizeof(exe);
    if (_NSGetExecutablePath(exe, &size) != 0) return 1;

    char resolved[PATH_MAX];
    if (realpath(exe, resolved) == NULL) return 1;

    /* resolved = <root>/Catlendar.app/Contents/MacOS/Catlendar -> up four levels */
    char root[PATH_MAX];
    strncpy(root, resolved, sizeof(root) - 1);
    root[sizeof(root) - 1] = '\0';
    for (int i = 0; i < 4; i++) {
        char tmp[PATH_MAX];
        strncpy(tmp, root, sizeof(tmp) - 1);
        tmp[sizeof(tmp) - 1] = '\0';
        char *parent = dirname(tmp);
        strncpy(root, parent, sizeof(root) - 1);
        root[sizeof(root) - 1] = '\0';
    }

    char python[PATH_MAX], pythonpath[PATH_MAX];
    snprintf(python, sizeof(python), "%s/.venv/bin/python3", root);
    snprintf(pythonpath, sizeof(pythonpath), "%s", root);

    if (access(python, X_OK) != 0) {
        fprintf(stderr, "Catlendar: interpreter missing at %s\n", python);
        return 2;
    }

    setenv("PYTHONPATH", pythonpath, 1);
    setenv("PYTHONUNBUFFERED", "1", 1);
    setenv("CATLENDAR_ROOT", root, 1);
    setenv("CATLENDAR_BUNDLED", "1", 1);
    if (chdir(root) != 0) return 3;

    char *args[8];
    int n = 0;
    args[n++] = python;
    args[n++] = "-m";
    args[n++] = "catlendar.app";
    for (int i = 1; i < argc && n < 7; i++) args[n++] = argv[i];
    args[n] = NULL;

    signal(SIGTERM, forward_signal);
    signal(SIGINT, forward_signal);

    if (posix_spawn(&child_pid, python, NULL, NULL, args, environ) != 0) {
        perror("Catlendar: posix_spawn");
        return 4;
    }

    int status = 0;
    while (waitpid(child_pid, &status, 0) < 0) {
        /* interrupted by a forwarded signal; keep waiting */
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
