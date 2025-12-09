from fastapi import FastAPI, Request
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from app.slack import create_app as create_slack_app

def create_api():
    slack_app = create_slack_app()
    handler = AsyncSlackRequestHandler(slack_app)

    api = FastAPI(
        title="Voice Sanitizer Backend",
    )

    @api.get("/")
    async def hello():
        return {"message": "Hello, world!"}
    
    @api.get("/slack/install")
    async def install(req: Request):
        return await handler.handle(req)


    @api.get("/slack/oauth_redirect")
    async def oauth_redirect(req: Request):
        return await handler.handle(req)

    return api

api = create_api()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        api,
        host="0.0.0.0",
        reload=True,
        port=3003,
    )
