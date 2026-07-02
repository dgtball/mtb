from fastapi import Request, HTTPException
from config import MINI_APP_SECRET

def check_token(request: Request) -> bool:
    token = request.headers.get("X-Mini-App-Token", "")
    return token == MINI_APP_SECRET

def require_token(request: Request):
    if not check_token(request):
        raise HTTPException(status_code=403, detail="Forbidden")
