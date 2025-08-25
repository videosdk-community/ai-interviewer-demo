import asyncio
import aiohttp
import os
from pathlib import Path
import sys
import argparse
from typing import AsyncIterator,Optional
from videosdk.agents import Agent, AgentSession, CascadingPipeline, function_tool, MCPServerStdio, MCPServerHTTP, JobContext, RoomOptions, WorkerJob, ConversationFlow, ChatRole
from videosdk.plugins.google import GoogleLLM, GoogleTTS, GoogleSTT
from videosdk.plugins.silero import SileroVAD
from videosdk import PubSubPublishConfig, PubSubSubscribeConfig
from videosdk.plugins.turn_detector import TurnDetector, pre_download_model
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
    conversation_flow = ConversationFlow(agent)
    pipeline = CascadingPipeline(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad,
        turn_detector=turn_detector
    )
    session = AgentSession(
        agent=agent,
        pipeline=pipeline,
        conversation_flow=conversation_flow
    )
    try:
        await ctx.connect()
        await session.start()
        await asyncio.Event().wait()
    finally:
        await session.close()
        await ctx.shutdown()
def make_context(meeting_id: Optional[str] = None) -> JobContext:
    resolved_meeting_id = meeting_id or os.getenv("MEETING_ID") or "Meeting-id"
    print("Using meeting ID:", resolved_meeting_id)
    room_options = RoomOptions(
        room_id=resolved_meeting_id,
        name="Cascading Agent",
        playground=False,
    )
    return JobContext(room_options=room_options)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start the VideoSDK AI Interview Agent")
    parser.add_argument(
        "-m",
        "--meeting-id",
        dest="meeting_id",
        help="Meeting ID to join/start the session (fallback: MEETING_ID env or 'Meeting-id')",
    )
    return parser.parse_args()
# Module-level storage for CLI-provided meeting ID to ensure picklable jobctx
CLI_MEETING_ID: Optional[str] = None


if __name__ == "__main__":
    args = parse_args()
    if args.meeting_id:
        os.environ["MEETING_ID"] = args.meeting_id
    job = WorkerJob(entrypoint=start_session, jobctx=make_context)
    job.start()