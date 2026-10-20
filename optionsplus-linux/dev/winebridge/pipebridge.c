/* Dev-only bridge: exposes a Windows named pipe (inside Wine) on a TCP port.
 * usage: pipebridge.exe <pipe-name> <tcp-port>
 * Each TCP client gets its own pipe connection; bytes are relayed both ways. */
#include <winsock2.h>
#include <windows.h>
#include <stdio.h>

typedef struct { HANDLE pipe; SOCKET sock; } link_t;

static DWORD WINAPI pipe_to_sock(LPVOID p) {
    link_t *l = p; char buf[65536]; OVERLAPPED ov = {0};
    ov.hEvent = CreateEventA(NULL, TRUE, FALSE, NULL);
    for (;;) {
        DWORD n = 0; ResetEvent(ov.hEvent);
        if (!ReadFile(l->pipe, buf, sizeof buf, NULL, &ov) && GetLastError() != ERROR_IO_PENDING) break;
        if (!GetOverlappedResult(l->pipe, &ov, &n, TRUE) || n == 0) break;
        if (send(l->sock, buf, (int)n, 0) <= 0) break;
    }
    shutdown(l->sock, SD_BOTH);
    return 0;
}

static DWORD WINAPI sock_to_pipe(LPVOID p) {
    link_t *l = p; char buf[65536]; OVERLAPPED ov = {0};
    ov.hEvent = CreateEventA(NULL, TRUE, FALSE, NULL);
    for (;;) {
        int n = recv(l->sock, buf, sizeof buf, 0);
        if (n <= 0) break;
        DWORD w = 0; ResetEvent(ov.hEvent);
        if (!WriteFile(l->pipe, buf, (DWORD)n, NULL, &ov) && GetLastError() != ERROR_IO_PENDING) break;
        if (!GetOverlappedResult(l->pipe, &ov, &w, TRUE)) break;
    }
    CancelIoEx(l->pipe, NULL);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <pipe> <port>\n", argv[0]); return 2; }
    WSADATA w; WSAStartup(MAKEWORD(2, 2), &w);
    SOCKET ls = socket(AF_INET, SOCK_STREAM, 0);
    struct sockaddr_in a = {0}; a.sin_family = AF_INET; a.sin_port = htons((u_short)atoi(argv[2]));
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    int one = 1; setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, (char *)&one, sizeof one);
    if (bind(ls, (struct sockaddr *)&a, sizeof a) || listen(ls, 4)) { fprintf(stderr, "bind failed\n"); return 1; }
    fprintf(stderr, "pipebridge: %s <-> 127.0.0.1:%s\n", argv[1], argv[2]);
    for (;;) {
        SOCKET s = accept(ls, NULL, NULL);
        if (s == INVALID_SOCKET) continue;
        HANDLE h = INVALID_HANDLE_VALUE;
        for (int i = 0; i < 300 && h == INVALID_HANDLE_VALUE; i++) {
            h = CreateFileA(argv[1], GENERIC_READ | GENERIC_WRITE, 0, NULL, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, NULL);
            if (h == INVALID_HANDLE_VALUE) { WaitNamedPipeA(argv[1], 1000); }
        }
        if (h == INVALID_HANDLE_VALUE) { fprintf(stderr, "pipe open failed %lu\n", GetLastError()); closesocket(s); continue; }
        fprintf(stderr, "pipebridge: connected\n");
        link_t *l = malloc(sizeof *l); l->pipe = h; l->sock = s;
        HANDLE t1 = CreateThread(NULL, 0, pipe_to_sock, l, 0, NULL);
        HANDLE t2 = CreateThread(NULL, 0, sock_to_pipe, l, 0, NULL);
        CloseHandle(t1); CloseHandle(t2);
    }
}
