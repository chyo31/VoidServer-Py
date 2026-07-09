import socket
import struct
import threading
import random
import json
import uuid
import time

# ---------- Minecraft 1.8 Constants ----------
PROTOCOL_VERSION = 47
SERVER_VERSION = "1.8.9"
DEFAULT_PORT = 25565

STATE_HANDSHAKE = 0
STATE_STATUS = 1
STATE_LOGIN = 2
STATE_PLAY = 3

# Packet IDs clientbound
JOIN_GAME          = 0x01
CHAT_MESSAGE       = 0x02
SPAWN_POSITION     = 0x05
PLAYER_POSITION    = 0x08
DISCONNECT_PLAY    = 0x40
PLAYER_ABILITIES   = 0x39

# Packet IDs serverbound
HANDSHAKE          = 0x00
LOGIN_START        = 0x00
CHAT_MESSAGE_SB    = 0x01
KEEP_ALIVE_SB      = 0x00
REQUEST            = 0x00
PING               = 0x01

# ---------- VarInt packing helpers ----------
def read_varint(data, offset):
    result = 0
    shift = 0
    while True:
        if offset == len(data):
            raise Exception("Buffer underflow")
        b = data[offset]
        offset += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if not (b & 0x80):
            break
    return result, offset

def write_varint(value):
    out = bytearray()
    while True:
        if value & ~0x7F:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        else:
            out.append(value & 0x7F)
            break
    return out

def write_string(s):
    encoded = s.encode('utf-8')
    return write_varint(len(encoded)) + encoded

def write_int(v):
    return struct.pack('>i', v)

def write_double(v):
    return struct.pack('>d', v)

def write_float(v):
    return struct.pack('>f', v)

def write_bool(v):
    return bytes([1 if v else 0])

def create_packet(packet_id, payload=b''):
    pid_bytes = write_varint(packet_id)
    total = pid_bytes + payload
    length_prefix = write_varint(len(total))
    return length_prefix + total

# ---------- Special packages ----------
def packet_keep_alive(keep_alive_id):
    # Untuk Minecraft 1.8, Payload dari Keep Alive adalah VarInt
    payload = write_varint(keep_alive_id)
    return create_packet(0x00, payload)

def packet_login_success(uuid_str, username):
    payload = write_string(uuid_str) + write_string(username)
    return create_packet(0x02, payload)

def packet_join_game(entity_id, gamemode=1, dimension=0, difficulty=1,
                     max_players=10, level_type='default', reduced_debug_info=False):
    payload = bytearray()
    payload += write_int(entity_id)
    payload += bytes([gamemode])
    payload += bytes([dimension])
    payload += bytes([difficulty])
    payload += bytes([max_players])
    payload += write_string(level_type)
    payload += write_bool(reduced_debug_info)
    return create_packet(JOIN_GAME, bytes(payload))

def packet_spawn_position(x, y, z):
    val = ((x & 0x3FFFFFF) << 38) | ((y & 0xFFF) << 26) | (z & 0x3FFFFFF)
    payload = struct.pack('>q', val)
    return create_packet(SPAWN_POSITION, payload)

def packet_player_position_and_look(x, y, z, yaw=0.0, pitch=0.0, flags=0x00):
    payload = bytearray()
    payload += write_double(x)
    payload += write_double(y)
    payload += write_double(z)
    payload += write_float(yaw)
    payload += write_float(pitch)
    payload += bytes([flags])
    return create_packet(PLAYER_POSITION, payload)

def packet_chat_message(message, position=0):
    payload = write_string(message) + bytes([position])
    return create_packet(CHAT_MESSAGE, payload)

def packet_player_abilities(gamemode, fly_speed=0.05, walk_speed=0.1):
    flags = 0x0F if gamemode == 1 else 0x00  # full creative
    payload = bytearray([flags]) + write_float(fly_speed) + write_float(walk_speed)
    return create_packet(PLAYER_ABILITIES, payload)

# ---------- Status JSON ----------
def status_response():
    resp = {
        "version": {
            "name": SERVER_VERSION,
            "protocol": PROTOCOL_VERSION
        },
        "players": {
            "max": 10,
            "online": len(clients),
            "sample": [{"name": c.name, "id": str(c.uuid)} for c in clients.values() if c.state == STATE_PLAY]
        },
        "description": {"text": "Server 1.8"}
    }
    return json.dumps(resp)

# ---------- Client Connection ----------
class ClientConnection(threading.Thread):
    def __init__(self, sock, addr):
        super().__init__(daemon=True)
        self.sock = sock
        self.addr = addr
        self.state = STATE_HANDSHAKE
        self.name = ""
        self.uuid = uuid.uuid4()
        self.entity_id = random.randint(100, 9999)
        self.running = True

    def send_packet(self, packet_bytes):
        try:
            self.sock.sendall(packet_bytes)
        except Exception:
            self.running = False

    def run(self):
        buffer = b''
        try:
            while self.running:
                data = self.sock.recv(8192)
                if not data:
                    break
                buffer += data
                while True:
                    if len(buffer) == 0:
                        break
                    try:
                        length, offset = read_varint(buffer, 0)
                        if offset + length > len(buffer):
                            break
                        packet_data = buffer[offset:offset+length]
                        buffer = buffer[offset+length:]
                        self.handle_packet(packet_data)
                    except Exception as e:
                        print(f"Error parsing packet: {e}")
                        self.running = False
                        break
        except (ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.cleanup()

    def handle_packet(self, data):
        if len(data) == 0:
            return
        packet_id, pos = read_varint(data, 0)
        payload = data[pos:]

        if self.state == STATE_HANDSHAKE and packet_id == HANDSHAKE:
            prot_ver, pos = read_varint(payload, 0)
            addr_len, pos = read_varint(payload, pos)
            # addr string is not required
            pos += addr_len
            port = struct.unpack('>H', payload[pos:pos+2])[0]
            pos += 2
            next_state, _ = read_varint(payload, pos)
            if next_state == 1:
                self.state = STATE_STATUS
            elif next_state == 2:
                self.state = STATE_LOGIN
            else:
                self.running = False
        elif self.state == STATE_STATUS:
            if packet_id == REQUEST:
                resp = create_packet(0x00, write_string(status_response()))
                self.send_packet(resp)
            elif packet_id == PING:
                if len(payload) >= 8:
                    self.send_packet(create_packet(0x01, payload[:8]))
                self.running = False
        elif self.state == STATE_LOGIN:
            if packet_id == LOGIN_START:
                name_len, pos = read_varint(payload, 0)
                self.name = payload[pos:pos+name_len].decode('utf-8')
                print(f"[+] {self.name} masuk dari {self.addr}")
                uuid_str = str(self.uuid)
                self.send_packet(packet_login_success(uuid_str, self.name))
                self.state = STATE_PLAY
                self.send_join_sequence()
        elif self.state == STATE_PLAY:
            self.handle_play_packet(packet_id, payload)

    def handle_play_packet(self, packet_id, payload):
        if packet_id == CHAT_MESSAGE_SB:
            msg_len, pos = read_varint(payload, 0)
            msg = payload[pos:pos+msg_len].decode('utf-8')
            print(f"[Chat] {self.name}: {msg}")
            self.broadcast_chat(msg)
        elif packet_id == KEEP_ALIVE_SB:
            # Keep alive reply from client, ignored
            pass

    def send_join_sequence(self):
        # Packets are sent with very small delays to avoid buffer overflow.
        self.send_packet(packet_join_game(self.entity_id, gamemode=1, dimension=0, difficulty=1))
        time.sleep(0.05)
        self.send_packet(packet_spawn_position(0, 64, 0))
        time.sleep(0.05)
        self.send_packet(packet_player_abilities(gamemode=1))
        time.sleep(0.05)
        self.send_packet(packet_player_position_and_look(0.0, 64.0, 0.0))

    def broadcast_chat(self, message):
        chat = json.dumps({"text": f"<{self.name}> {message}"})
        packet = packet_chat_message(chat, position=0)
        with clients_lock:
            for c in clients.values():
                if c is not self and c.state == STATE_PLAY:
                    c.send_packet(packet)

    def cleanup(self):
        print(f"[-] {self.name or self.addr} disconnected")
        self.running = False
        with clients_lock:
            if self.addr in clients:
                del clients[self.addr]
        try:
            self.sock.close()
        except:
            pass

# ---------- Server ----------
clients = {}
clients_lock = threading.Lock()

def keep_alive_loop():
    while True:
        time.sleep(15)  # Send every 15 seconds to prevent 30 second timeout
        keep_alive_id = random.randint(1, 2147483647)
        packet = packet_keep_alive(keep_alive_id)
        
        with clients_lock:
            for c in list(clients.values()):
                if c.state == STATE_PLAY:
                    c.send_packet(packet)

def main():
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind(('0.0.0.0', DEFAULT_PORT))
    server_sock.listen(5)
    print(f"Minecraft Java 1.8 Server running on port {DEFAULT_PORT}")
    print("Void world, creative mode")

    threading.Thread(target=keep_alive_loop, daemon=True).start()

    try:
        while True:
            client_sock, addr = server_sock.accept()
            print(f"connection from {addr}")
            conn = ClientConnection(client_sock, addr)
            with clients_lock:
                clients[addr] = conn
            conn.start()
    except KeyboardInterrupt:
        print("Server is shut down.")
    finally:
        server_sock.close()

if __name__ == '__main__':
    main()