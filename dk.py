import bcrypt
print(bcrypt.hashpw("999".encode(), bcrypt.gensalt()).decode())