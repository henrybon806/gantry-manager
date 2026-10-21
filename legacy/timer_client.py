import socket
import sys

HOST, PORT = "127.0.0.1", 5050
# cmd = " ".join(sys.argv[1:]) or input("Command: ")
cmd = "Status"
with socket.create_connection((HOST, PORT)) as s:
    s.sendall((cmd + "\n").encode())
    print(s.recv(1024).decode().strip())