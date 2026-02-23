import bcrypt

new_password = "Admin@123"  # अपना नया password
hashed = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
print(hashed)