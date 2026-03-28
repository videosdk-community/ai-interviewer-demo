import asyncio
import aiohttp
import os
from pathlib import Path
import sys
import argparse
from typing import AsyncIterator,Optional
from videosdk.agents import Agent, AgentSession, Pipeline, function_tool, MCPServerStdio, MCPServerHTTP, JobContext, RoomOptions, WorkerJob
from videosdk.plugins.google import GoogleLLM, GoogleTTS, GoogleSTT
from videosdk.plugins.silero import SileroVAD
from videosdk import PubSubPublishConfig, PubSubSubscribeConfig
from videosdk.plugins.turn_detector import TurnDetector, pre_download_model

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", handlers=[logging.StreamHandler()])

# Pre-downloading the Turn Detector model
pre_download_model()
class MyVoiceAgent(Agent):
    def __init__(self, ctx: Optional[JobContext] = None):
        #mcp_script = Path(__file__).parent.parent / "MCP Server" / "mcp_stdio_example.py"
        super().__init__(
            instructions="You are VideoSDK's AI Interview Agent. You take interview for the Full Stack developer role. Send the question that you ask through pubsub message. You must not give any answers to the candidate. If candidate asks for help or clarification say I can't help you with that. Please proceed.",
            #tools=[get_weather],
        )
        self.ctx=ctx
    async def on_enter(self) -> None:
        await self.session.say("Hello, Welcome to your interview.")
    async def on_exit(self) -> None:
        await self.session.say("Goodbye!")
    @function_tool
    async def send_pubsub_message(self, message: str):
        """Send a message to the pubsub topic CHAT_MESSAGE"""
        publish_config = PubSubPublishConfig(
            topic="CHAT",
            message=message
        )
        await self.ctx.room.publish_to_pubsub(publish_config)
        return "Message sent to pubsub topic CHAT_MESSAGE"
    @function_tool
    async def end_call(self) -> None:
        """End the call upon request by the user"""
        await self.session.say("Goodbye!")
        await asyncio.sleep(1)
        await self.session.leave()
async def start_session(ctx: JobContext):
    # STT Providers
    stt=GoogleSTT( model="latest_long")
    llm=GoogleLLM(api_key=os.getenv("GOOGLE_API_KEY"))
    tts=GoogleTTS(api_key=os.getenv("GOOGLE_API_KEY"))
    vad = SileroVAD()
    turn_detector = TurnDetector(threshold=0.8)
    agent = MyVoiceAgent(ctx)
    pipeline = Pipeline(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        turn_detector=turn_detector
    )
    session = AgentSession(
        agent=agent,
        pipeline=pipeline
    )
    try:
        await ctx.connect()
        await session.start()
        await asyncio.Event().wait()
    finally:
        await session.close()
        await ctx.shutdown()
def make_context(room_id: Optional[str] = None) -> JobContext:
    resolved_room_id = room_id or os.getenv("ROOM_ID") or "<room_id>"
    print("Using room ID:", resolved_room_id)
    room_options = RoomOptions(
        room_id=resolved_room_id,
        name="Cascading Agent",
        playground=False,
    )
    return JobContext(room_options=room_options)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the VideoSDK AI Interview Agent")
    parser.add_argument(
        "-m",
        "--room-id",
        dest="room_id",
        help="Room ID to join/start the session (fallback: ROOM_ID env or '<room_id>')",
    )
    return parser.parse_args()
# Module-level storage for CLI-provided room ID to ensure picklable jobctx
CLI_ROOM_ID: Optional[str] = None


if __name__ == "__main__":
    args = parse_args()
    if args.room_id:
        os.environ["ROOM_ID"] = args.room_id
    job = WorkerJob(entrypoint=start_session, jobctx=make_context)
    job.start()