import os
import uvicorn

# Render espone la porta in env PORT
PORT = int(os.environ.get("PORT", "8000"))

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=PORT, log_level="info")
