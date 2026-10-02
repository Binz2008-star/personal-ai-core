import configparser

parser = configparser.ConfigParser()
parser.read("config/app.ini", encoding="utf-8")
port = parser.getint("server", "port")
if port != 8081:
    raise SystemExit(f"port is {port}, expected 8081")
print("check passed: port 8081")
