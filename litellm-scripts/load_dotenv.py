import os

def load_dotenv(file_path=".env"):
    if not os.path.exists(file_path):
        return

    with open(file_path, "r") as f:
        for line in f:
            # Clean up whitespace and skip empty lines or comments
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            
            # Split by the first '=' found
            if "=" in line:
                key, value = line.split("=", 1)
                
                # Remove optional quotes around values
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                
                os.environ[key] = value
