import asyncio
from asyncio import (
    AbstractEventLoop,
    Future,
    Task,
    wait_for,
    TimeoutError,
)
from dataclasses import dataclass
import json
import logging
import time
from ._events import Events
from typing import Any, Awaitable, Dict, Optional, Union, TypedDict, TypeVar
from random import random
from pyee import AsyncIOEventEmitter
from websockets import connect
from .exceptions import InternalServerError
from vsaiortc.mediastreams import AudioStreamTrack, MediaStreamTrack, VideoStreamTrack
from vsaiortc.rtcconfiguration import RTCIceServer
from .lib.pymediasoup.consumer import Consumer
from .lib.pymediasoup.device import Device
from .lib.pymediasoup.handlers.aiortc_handler import AiortcHandler
from .lib.pymediasoup.models.transport import DtlsParameters
from .lib.pymediasoup.producer import Producer
from .lib.pymediasoup.rtp_parameters import RtpParameters
from .lib.pymediasoup.transport import Transport
from .utils import (
    PubSubPublishConfig,
    PubSubSubscribeConfig,
    RecordingConfig,
    ServerConfig,
    TranscriptionConfig,
    get_server_config,
)


def generateRandomNumber():
    return round(random() * 10000000)


T = TypeVar("T")
logger = logging.getLogger(__name__)

# Tasks
TASK_INCOMING_MESSAGES = "TASK_INCOMING_MESSAGES"
TASK_JOIN = "TASK_JOIN"
TASK_LEAVE = "TASK_LEAVE"
TASK_CLOSE = "TASK_CLOSE"
TASK_SEND_MESSAGES = "TASK_SEND_MESSAGES"
TASK_ANY = "TASK_ANY"


@dataclass
class RoomClientConfig(TypedDict, total=False):
    room_id: str
    peer_id: str
    secret: str
    display_name: str
    mode: str
    produce: bool
    consume: bool
    use_sharing_simulcast: bool
    datachannel: bool
    mic_enabled: bool
    webcam_enabled: bool
    custom_camera_video_track: Optional[VideoStreamTrack]
    custom_microphone_audio_track: Optional[AudioStreamTrack]
    auto_consume: bool
    preferred_protocol: str
    signaling_base_url: str
    meta_data: Optional[Dict[str, Any]]
    default_camera_index: int
    debug_mode: bool
    peer_type: Optional[str]
    buffer_size: Optional[int]
    peer_type: Optional[str]
    loop: AbstractEventLoop
    sdk_metadata: Optional[Dict[str, Any]]


class RoomClient(AsyncIOEventEmitter):
    def __init__(self, **kwargs: Union[RoomClientConfig, Any]):
        """
        Initialize RoomClient with configuration parameters.
        """
        super(RoomClient, self).__init__(loop=kwargs.get("loop"))
        self.__loop: AbstractEventLoop = kwargs.get("loop") or asyncio.get_event_loop()
        if self.__loop is None:
            try:
                self.__loop = asyncio.get_event_loop()
            except RuntimeError as e:
                raise InternalServerError(e)
        self.__room_id: str = kwargs.get("room_id")
        self.__peer_id: str = kwargs.get("peer_id")
        self.__secret: str = kwargs.get("secret")
        self.__signaling_base_url: str = kwargs.get("signaling_base_url")
        self._base_url: str = None
        self.__device: Device = None
        self.__mode: str = kwargs.get("mode")
        self.__peer_type: str = kwargs.get("peer_type", "normal")
        self.__produce: bool = kwargs.get("produce")
        self.__consume: bool = kwargs.get("consume")
        self.__use_sharing_simulcast: bool = kwargs.get("use_sharing_simulcast")
        self.__use_datachannel: bool = kwargs.get("datachannel")

        self.__auto_consume: bool = kwargs.get("auto_consume")
        self.__peer_type: str = kwargs.get("peer_type", "normal")
        self.__preferred_protocol: str = kwargs.get("preferred_protocol")
        self.__debug_mode: bool = kwargs.get("debug_mode")
        self.__sdk_version: str = "0.2.0"
        self.__sdk_name: str = "python"
        self.__sdk_metadata: Dict[str, Any] = kwargs.get("sdk_metadata", {})

        self.__generate_random_number = generateRandomNumber
        self.__websocket = None
        self.__tasks: dict[str, Task] = {}
        self.__answers: Dict[str, Future] = {}
        self.__attributes: Dict[str, Any] = {}
        self.__send_transport: Optional[Transport] = None
        self.__recv_transport: Optional[Transport] = None
        self.__ice_servers: list[RTCIceServer] = []

        self.__custom_camera_video_track: Optional[VideoStreamTrack] = kwargs.get(
            "custom_camera_video_track"
        )
        self.__custom_microphone_audio_track: Optional[AudioStreamTrack] = kwargs.get(
            "custom_microphone_audio_track"
        )

        self.__buffer_size: Optional[int] = kwargs.get("buffer_size", 5)
        self.__audio_stream_track = (
            AudioStreamTrack()
            if self.__custom_microphone_audio_track is None
            else self.__custom_microphone_audio_track
        )
        self.__video_stream_track = (
            VideoStreamTrack()
            if self.__custom_camera_video_track is None
            else self.__custom_camera_video_track
        )

        self.__producers = []
        self.__consumers: dict[str, Consumer] = {}
        self.__audio_producer = None
        self.__video_producer = None

        self.display_name: str = kwargs.get("display_name")
        self.mic_enabled: bool = kwargs.get("mic_enabled")
        self.webcam_enabled: bool = kwargs.get("webcam_enabled")

        self.meta_data: Optional[Dict[str, Any]] = kwargs.get("meta_data")
        self.default_camera_index: int = kwargs.get("default_camera_index")
        self.session_id: str = None
        self._closed = False
        self._joined = False

        logger.debug("mic_enabled: %s", self.mic_enabled)
        logger.debug("webcam_enabled: %s", self.webcam_enabled)
        logger.debug("consume: %s", self.__consume)
        logger.debug("produce: %s", self.__produce)
        logger.debug(f"Meeting Room Client initialized with room_id: {self.__room_id}")

    def join(self):
        """
        Join the meeting room.
        """
        try:
            self.__tasks[TASK_JOIN] = asyncio.ensure_future(self.async_join())
        except Exception as e:
            logger.error(f"Failed to Join :: {e}")
            self.__emit_error("Failed to Join in the Meeting")

    async def async_join(self):
        """
        Asynchronously join the meeting room.
        """
        try:
            if not self._joined:
                self._joined = True
                server_config: ServerConfig = get_server_config(
                    self.__room_id, self.__secret, self.__signaling_base_url
                )
                base_url = server_config["base_url"]
                self._base_url = base_url
                signaling_url = server_config["signaling_url"]
                self.__ice_servers = server_config["ice_servers"]
                self.__attributes = {
                    "logs": server_config["logs"],
                    "observability": server_config["observability_Jwt"],
                    "traces": server_config["traces"],
                }
                self.emit(Events.MEETING_STATE_CHANGED, {"state": "CONNECTING"})

                websocket_url = (
                    signaling_url
                    if signaling_url is not None
                    else f"wss://{base_url}/?roomId={self.__room_id}&peerId={self.__peer_id}&secret={self.__secret}&mode={self.__mode}&peerType={self.__peer_type}"
                )

                self.__websocket = await connect(
                    websocket_url, subprotocols=["protoo"], ping_interval=2000
                )

                self.emit(Events.MEETING_STATE_CHANGED, {"state": "CONNECTED"})

                # create thread
                self.__tasks[TASK_INCOMING_MESSAGES] = asyncio.ensure_future(
                    self.__handle_incoming_messages()
                )

                await self.handle_device_load()
                await self.handle_send_transport()
                await self.handle_recv_transport()

                await self.handle_request_entry()
                joined_data = await self.handle_join()

                if self.__produce:
                    await self.handle_produce()

                logger.debug("Meeting joined")
                self.emit(
                    Events.MEETING_JOINED,
                    {
                        "participants": joined_data["data"]["peers"],
                        "messages": joined_data["data"]["messages"],
                        "base_url": self._base_url,
                    },
                )

        except Exception as e:
            logger.debug("ERROR", e)
            self.emit(Events.MEETING_STATE_CHANGED, {"state": "FAILED"})
            if self.__websocket:
                await self.__websocket.close()
            if self.__send_transport:
                await self.__send_transport.close()
            if self.__recv_transport:
                await self.__recv_transport.close()

            logger.error(f"Failed to Join :: {e}")
            self.__emit_error("Failed to Join in the Meeting")

    async def __send_request(self, request):
        self.__answers[request["id"]] = asyncio.get_event_loop().create_future()
        await self.__websocket.send(json.dumps(request))

    async def __wait_for(
        self, fut: Awaitable[T], timeout: Optional[float], **kwargs: Any
    ) -> T:
        try:
            return await wait_for(fut, timeout=timeout, **kwargs)
        except TimeoutError:
            logger.error(f"signal :: Timeout")
            self.__emit_error("Operation timed out")

    async def __handle_incoming_messages(self):
        try:
            async for Data in self.__websocket:
                message: Dict[str, Any] = json.loads(Data)
                if message.get("response"):
                    self.__handle_responses(message)
                elif message.get("request"):
                    await self.__handle_requests(message)
                elif message.get("notification"):
                    await self.__handle_notifications(message)
        except Exception as e:
            logger.error(f"signal :: error while handling signal messages :: {e}")
            self.__emit_error("Internal socket Error")

    def __handle_responses(self, message: Dict[str, Any]):
        try:
            logger.debug(f"signal :: response message :: {message}")
            if message.get("id") is not None:
                self.__answers[message.get("id")].set_result(message)
        except Exception as e:
            logger.error(f"signal :: error while handling signal messages :: {e}")
            self.__emit_error("Internal socket Error")

    async def __handle_requests(self, message: Dict[str, Any]):
        try:
            logger.debug(f"signal :: request message :: {message}")
            if message.get("method") == "close":
                self.leave()
            if message.get("method") == "newConsumer":
                if self.__consume:
                    await self.handleConsume(
                        id=message["data"]["id"],
                        producerId=message["data"]["producerId"],
                        kind=message["data"]["kind"],
                        rtpParameters=message["data"]["rtpParameters"],
                        peerId=message["data"]["peerId"],
                        appData=message["data"]["appData"],
                    )
                    await self.__accept(message=message)
                else:
                    logger.debug("I don't want to consume newConsumer event")
                    await self.__reject(
                        message=message["data"],
                        code=403,
                        reason="I don't want to consume",
                    )

            if message.get("method") == "newDataConsumer":
                if self.__consume:
                    await self.__accept(message=message)
                else:
                    logger.debug("I don't want to consume newDataConsumer event")
                    await self.__reject(
                        message=message["data"],
                        code=403,
                        reason="I don't want to consume",
                    )

            if message.get("method") == "enabledMic":
                logger.debug("signal :: enable mic received")
                self.emit(Events.MIC_REQUESTED, message["data"])
                await self.__accept(message=message)

            if message.get("method") == "disableMic":
                logger.debug("signal :: disable mic received")
                await self.async_disable_mic()
                await self.__accept(message=message)

            if message.get("method") == "enableWebcam":
                logger.debug("signal :: enable webcam received")
                self.emit(Events.WEBCAM_REQUESTED, message["data"])
                await self.__accept(message=message)

            if message.get("method") == "disableWebcam":
                logger.debug("signal :: disable webcam received")
                await self.async_disable_webcam()
                await self.__accept(message=message)
        except Exception as e:
            logger.error(f"signal :: Error while websocket request: {e}")
            self.__emit_error("Internal socket Error")

    async def __handle_notifications(self, message: Dict[str, Any]):
        try:
            logger.debug(f"signal :: notification message :: {message}")

            if message["method"] == "entryResponded":
                self.session_id = message["data"]["sessionId"]

            if message["method"] == "producerScore":
                producerId = message["data"]["producerId"]
                score = message["data"]["score"]
                logger.debug(f"signal :: producer score {producerId} :: {score}")

            if message["method"] == "newPeer":
                peerId = message["data"]["id"]
                logger.debug(f"signal :: peer connected {peerId}")
                self.emit(Events.ADD_PEER, message["data"])

            if message["method"] == "peerClosed":
                peerId = message["data"]["peerId"]
                logger.debug(f"signal :: peer disconnected {peerId}")
                self.emit(Events.REMOVE_PEER, message["data"])

            if message["method"] == "consumerClosed":
                consumerId = message["data"]["consumerId"]
                logger.debug(f"signal :: consumer closed {consumerId}")
                if consumerId in self.__consumers:
                    consumer = self.__consumers.pop(consumerId)
                    self.emit(Events.REMOVE_CONSUMER, consumer)
                    del consumer

            if message["method"] == "consumerPaused":
                consumerId = message["data"]["consumerId"]
                logger.debug(f"signal :: consumer paused {consumerId}")

            if message["method"] == "consumerResumed":
                consumerId = message["data"]["consumerId"]
                logger.debug(f"signal :: consumer resumed {consumerId}")

            if message["method"] == "consumerLayersChanged":
                consumerId = message["data"]["consumerId"]
                logger.debug(f"signal :: consumer layers changed {consumerId}")
                if consumerId in self.__consumers:
                    consumer: Consumer = self.__consumers.get(consumerId)
                    if len(consumer.appData["encodings"]) > 1:
                        self.emit(
                            Events.VIDEO_QUALITY_CHANGED,
                            {
                                "peerId": consumer.appData["peerId"],
                                "prevQuality": "HIGH",
                                "currentQuality": "HIGH",
                            },
                        )

            if message["method"] == "activeSpeaker":
                logger.debug(f"signal :: active speaker changed")
                self.emit(Events.SET_ROOM_ACTIVE_SPEAKER, message["data"])
            if message["method"] == "participantMediaStateChanged":
                logger.debug(
                    f"signal :: participant media state changed {message['data']}"
                )
                self.emit(Events.PARTICIPANT_MEDIA_STATE_CHANGED, message["data"])

            # recording
            if message["method"] == "recordingStateChanged":
                logger.debug(f"signal :: recording state changed {message['data']}")
                self.emit(Events.RECORDING_STATE_CHANGED, message["data"])

            if message["method"] == "recordingStarted":
                logger.debug(f"signal :: recording started {message['data']}")
                self.emit(Events.RECORDING_STARTED, message["data"])

            if message["method"] == "recordingStopped":
                logger.debug(f"signal :: recording stopped {message['data']}")
                self.emit(Events.RECORDING_STOPPED, message["data"])

            # realtime transcription
            if message["method"] == "transcriptionStateChanged":
                logger.debug(f"signal :: transcription state changed {message['data']}")
                self.emit(Events.TRANSCRIPTION_STATE_CHANGED, message["data"])

            if message["method"] == "transcriptionText":
                logger.debug(f"signal :: recording started {message['data']}")
                self.emit(Events.TRANSCRIPTION_TEXT, message["data"])

            # pubsub
            if message["method"] == "pubsubMessage":
                logger.debug(f"signal :: pubsub message {message['data']}")
                self.emit(Events.PUBSUB_MESSAGE, message["data"])

        except Exception as e:
            logger.error(
                f"signal :: Error processing websocket notification :: {e}",
                exc_info=True,
            )
            self.__emit_error("Internal socket Error")

    async def handle_device_load(self):
        try:
            # Init device
            self.__device = Device(
                handlerFactory=AiortcHandler.createFactory(
                    tracks=[self.__audio_stream_track, self.__video_stream_track]
                )
            )

            # Get Router RtpCapabilities
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "getRouterRtpCapabilities",
                "data": {},
            }
            await self.__send_request(req)
            ans = await self.__wait_for(self.__answers[reqId], timeout=15)

            # Load Router RtpCapabilities
            await self.__device.load(ans["data"])

            logger.debug(f"RTC :: device loaded at ::  {time.time()}")
        except Exception as e:
            logger.error(f"RTC :: error while device loading :: {e}")
            self.__emit_error("Error while Device Load")

    async def handle_send_transport(self):
        if self.__send_transport != None:
            logger.debug("RTC :: Send Transport is already created")
            return
        try:
            # Send create sendTransport request
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "createWebRtcTransport",
                "data": {
                    "preferredProtocol": self.__preferred_protocol,
                    "forceTcp": False,
                    "producing": True,
                    "consuming": False,
                    "sctpCapabilities": (
                        self.__device.sctpCapabilities.dict()
                        if self.__use_datachannel
                        else None
                    ),
                },
            }
            await self.__send_request(req)
            ans = await self.__wait_for(self.__answers[reqId], timeout=15)

            # Create sendTransport
            self.__send_transport = self.__device.createSendTransport(
                id=ans["data"]["id"],
                iceParameters=ans["data"]["iceParameters"],
                iceCandidates=ans["data"]["iceCandidates"],
                dtlsParameters=ans["data"]["dtlsParameters"],
                sctpParameters=ans["data"]["sctpParameters"],
                iceServers=self.__ice_servers,
            )
            logger.debug(f"RTC :: send transport created {time.time()}")
        except Exception as e:
            logger.error(f"RTC :: error while send createWebRtcTransport() :: {e}")
            self.__emit_error("Error while Creating Send Transport")

        @self.__send_transport.on("connect")
        async def on_connect(dtlsParameters: DtlsParameters):
            try:
                logger.debug(f"RTC :: send transport connecting at ::  {time.time()}")
                reqId = self.__generate_random_number()
                req = {
                    "request": True,
                    "id": reqId,
                    "method": "connectWebRtcTransport",
                    "data": {
                        "transportId": self.__send_transport.id,
                        "dtlsParameters": dtlsParameters.dict(exclude_none=True),
                    },
                }
                await self.__send_request(req)
                ans = await self.__wait_for(self.__answers[reqId], timeout=15)
                logger.debug(f"RTC :: send transport connected at ::  {time.time()}")
            except Exception as e:
                logger.error(f"RTC :: error while send connectWebRtcTransport() :: {e}")
                self.__emit_error("Error while Connecting Send Transport")

        @self.__send_transport.on("produce")
        async def on_produce(kind: str, rtpParameters: RtpParameters, appData: dict):
            try:
                reqId = self.__generate_random_number()
                req = {
                    "id": reqId,
                    "method": "produce",
                    "request": True,
                    "data": {
                        "transportId": self.__send_transport.id,
                        "kind": kind,
                        "rtpParameters": rtpParameters.dict(exclude_none=True),
                        "appData": appData,
                    },
                }
                await self.__send_request(req)
                ans = await self.__wait_for(self.__answers[reqId], timeout=15)
                return ans["data"]["id"]
            except Exception as e:
                logger.error(f"RTC :: error while produce() :: {e}")
                self.__emit_error("Error while Producing Media")

        logger.debug(f"RTC :: send transport handled at ::  {time.time()}")

    async def handle_request_entry(self):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "requestEntry",
                "data": {
                    "name": self.display_name,
                },
            }
            await self.__send_request(req)
            logger.debug(f"RTC :: request entry sent {time.time()}")
        except Exception as e:
            logger.error(f"RTC :: error while request entry :: {e}")
            self.__emit_error("Error while Requesting Entry")

    async def handle_join(self):
        try:
            # Join room
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "join",
                "data": {
                    "autoConsume": self.__auto_consume,
                    "secret": self.__secret,
                    "displayName": self.display_name,
                    "device": "Unknown",
                    "deviceInfo": {
                        "sdkType": self.__sdk_name,
                        "sdkVersion": self.__sdk_version,
                        "platform": "server",
                        "sdkMetadata": self.__sdk_metadata,
                    },
                    "rtpCapabilities": self.__device.rtpCapabilities.dict(
                        exclude_none=True
                    ),
                    "sctpCapabilities": self.__device.sctpCapabilities.dict(
                        exclude_none=True
                    ),
                    "metaData": self.meta_data,
                },
            }
            await self.__send_request(req)
            ans = await self.__wait_for(self.__answers[reqId], timeout=15)

            logger.debug(f"RTC :: joined into meeting {time.time()}")
            return ans
        except Exception as e:
            logger.error(f"RTC :: error while join() :: {e}")
            self.__emit_error("Error while Join")

    async def handle_produce(self):
        # produce
        try:
            if self.webcam_enabled:
                await self.async_enable_webcam()
                logger.debug(
                    "RTC :: sendtransport :: videoProducer created successfully..."
                )
        except Exception as e:
            logger.error(f"RTC :: error while create video producer :: {e}")
            self.__emit_error("Error while Producing Video")

        try:
            if self.mic_enabled:
                await self.async_enable_mic()
                logger.debug(
                    "RTC :: sendtransport :: audioProducer created successfully..."
                )
        except Exception as e:
            logger.error(f"RTC :: error while creating audio producer :: {e}")
            self.__emit_error("Error while Producing Audio")

    async def handle_recv_transport(self):
        if self.__recv_transport != None:
            return
        try:
            # Send create recvTransport request
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "createWebRtcTransport",
                "data": {
                    "preferredProtocol": self.__preferred_protocol,
                    "forceTcp": False,
                    "producing": False,
                    "consuming": True,
                    "sctpCapabilities": (
                        self.__device.sctpCapabilities.dict()
                        if self.__use_datachannel
                        else None
                    ),
                },
            }
            await self.__send_request(req)
            ans = await self.__wait_for(self.__answers[reqId], timeout=15)

            # Create recvTransport
            self.__recv_transport = self.__device.createRecvTransport(
                id=ans["data"]["id"],
                iceParameters=ans["data"]["iceParameters"],
                iceCandidates=ans["data"]["iceCandidates"],
                dtlsParameters=ans["data"]["dtlsParameters"],
                sctpParameters=ans["data"]["sctpParameters"],
                iceServers=self.__ice_servers,
            )

            logger.debug(f"RTC :: recv transport created {time.time()}")
        except Exception as e:
            logger.error(f"RTC :: error while recv createWebRtcTransport() :: {e}")
            self.__emit_error("Error while Creating Recv Transport")

        @self.__recv_transport.on("connect")
        async def on_connect(dtlsParameters: DtlsParameters):
            try:
                logger.debug(
                    f"RTC :: recv WebRtcTransport connectWebRtcTransport {time.time()}"
                )
                reqId = self.__generate_random_number()
                req = {
                    "request": True,
                    "id": reqId,
                    "method": "connectWebRtcTransport",
                    "data": {
                        "transportId": self.__recv_transport.id,
                        "dtlsParameters": dtlsParameters.dict(exclude_none=True),
                    },
                }
                await self.__send_request(req)
                ans = await self.__wait_for(self.__answers[reqId], timeout=15)
            except Exception as e:
                logger.error(f"RTC :: error while recv connectWebRtcTransport() :: {e}")
                self.__emit_error("Error while Connecting Recv Transport")

        logger.debug(f"RTC :: recv transport handled at ::  {time.time()}")

    async def handleConsume(
        self,
        id,
        producerId,
        kind,
        rtpParameters: Union[RtpParameters, dict],
        peerId,
        appData,
    ) -> Consumer:
        try:
            if self.__recv_transport == None:
                await self.handle_recv_transport()
            logger.debug(f"creating {kind} consumer for participant :: {peerId}")
            consumer: Consumer = await self.__recv_transport.consume(
                id=id,
                producerId=producerId,
                kind=kind,
                rtpParameters=rtpParameters,
                appData=appData,
                track_buffer_size=self.__buffer_size,
            )

            @consumer.on("transportClose")
            def on_consumer_transport_closed():
                logger.debug(f"consumer transport closed {consumer.id}")

            @consumer.observer.on("close")
            def on_consumer_closed():
                logger.debug(f"consumer closed {consumer.id}")

            logger.debug(f"created {kind} consumer for participant :: {peerId}")

            self.__consumers[consumer.id] = consumer

            if consumer.track.kind == "audio":
                consumer.track.kind = (
                    "shareAudio" if "share" in consumer.appData else consumer.track.kind
                )
            else:
                consumer.track.kind = (
                    "share" if "share" in consumer.appData else consumer.track.kind
                )

            self.emit(Events.ADD_CONSUMER, consumer)

        except Exception as e:
            logger.error(f"RTC :: error while Creating Consumer :: {e}")
            self.__emit_error("Error while Consume")

    async def __accept(self, message):
        response = {
            "response": True,
            "id": message["id"],
            "ok": True,
            "data": {},
        }
        await self.__websocket.send(json.dumps(response))

    async def __reject(self, message, code, reason):
        response = {
            "response": True,
            "id": message["id"],
            "ok": False,
            "errorCode": code,
            "errorReason": reason,
        }
        await self.__websocket.send(json.dumps(response))

    def __emit_error(self, message: str):
        self.emit(Events.ERROR, message)

    def enable_webcam(self, custom_track: Optional[MediaStreamTrack]):
        logger.debug(f"RTC :: webcam_enabled {self.webcam_enabled}")
        if not self.webcam_enabled:
            self.webcam_enabled = True

        if self.webcam_enabled:
            self.__tasks[f"{TASK_ANY}-enable-webcam"] = asyncio.ensure_future(
                self.async_enable_webcam(custom_track)
            )

    async def async_enable_webcam(
        self, custom_track: Optional[MediaStreamTrack] = None
    ):
        try:
            logger.debug(f"RTC :: webcam_enabled {self.webcam_enabled}")
            if not self.webcam_enabled:
                self.webcam_enabled = True

            if self.__video_producer is not None:
                raise Exception(
                    "Webcam Already Enabled, Disable Webcam First to Enable New Video"
                )

            if custom_track is not None:
                if self.__video_producer is not None:
                    await self.__video_producer.close()

                self.__video_producer: Producer = await self.__send_transport.produce(
                    track=custom_track, stopTracks=True, appData={}
                )
            else:
                if (
                    self.__video_stream_track.readyState == "ended"
                    and self.__custom_camera_video_track is None
                ):
                    self.__video_stream_track = VideoStreamTrack()
                self.__video_producer: Producer = await self.__send_transport.produce(
                    track=self.__video_stream_track,
                    stopTracks=False,
                    appData={},
                )
            logger.debug(f"RTC :: video producer created :: {time.time()}")

            @self.__video_producer.on("transportclose")
            def on_producer_transport_closed():
                logger.debug(
                    f"video producer transport closed {self.__video_producer.id}"
                )
                self.emit(Events.REMOVE_PRODUCER, self.__video_producer)

            @self.__video_producer.observer.on("close")
            def on_producer_close():
                logger.debug(f"video producer close :: {self.__video_producer.id}")

            @self.__video_producer.observer.on("pause")
            def on_producer_close():
                logger.debug(f"video producer pause :: {self.__video_producer.id}")

            @self.__video_producer.observer.on("resume")
            def on_producer_close():
                logger.debug(f"video producer resume :: {self.__video_producer.id}")

            self.emit(Events.ADD_PRODUCER, self.__video_producer)
            self.__producers.append(self.__video_producer)
        except Exception as e:
            logger.error(f"MEDIA :: error while Enable Webcam :: {e}")
            self.__emit_error("Not Able to Enable Webcam")

    def disable_webcam(self):
        logger.debug(f"RTC :: webcam_enabled {self.webcam_enabled}")
        if self.webcam_enabled:
            self.__tasks[f"{TASK_ANY}-disable-webcam"] = asyncio.ensure_future(
                self.async_disable_webcam()
            )

    async def async_disable_webcam(self):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "closeProducer",
                "data": {"producerId": self.__video_producer.id},
            }

            await self.__video_producer.close()
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)

            self.emit(Events.REMOVE_PRODUCER, self.__video_producer)
            self.__producers.remove(self.__video_producer)
            self.__video_producer = None
        except Exception as e:
            logger.error(f"MEDIA :: error while Disable Webcam :: {e}")
            self.__emit_error("Not Able to Disbale Webcam")

    def enable_mic(self, custom_track: Optional[MediaStreamTrack] = None):
        logger.debug(f"RTC :: mic_enabled {self.mic_enabled}")
        if not self.mic_enabled:
            self.mic_enabled = True

        if self.mic_enabled:
            self.__tasks[f"{TASK_ANY}-enable-mic"] = asyncio.ensure_future(
                self.async_enable_mic(custom_track)
            )

    async def async_enable_mic(self, custom_track: Optional[MediaStreamTrack] = None):
        try:
            logger.debug(f"RTC :: mic_enabled {self.mic_enabled}")
            if not self.mic_enabled:
                self.mic_enabled = True

            if self.__audio_producer is not None:
                raise Exception(
                    "Mic Already Enabled, Disable Mic First to Enable New Audio"
                )

            if custom_track is not None:
                self.__audio_producer: Producer = await self.__send_transport.produce(
                    track=custom_track, stopTracks=False, appData={}
                )
            else:
                # if it is already ended create new one
                if (
                    self.__audio_stream_track.readyState == "ended"
                    and self.__custom_microphone_audio_track is None
                ):
                    self.__audio_stream_track = AudioStreamTrack()
                self.__audio_producer: Producer = await self.__send_transport.produce(
                    track=self.__audio_stream_track,
                    appData={},
                )
            logger.debug(f"RTC :: audio producer created :: {time.time()}")

            @self.__audio_producer.on("transportclose")
            def on_producer_transport_closed():
                logger.debug(
                    f"audio producer transport closed {self.__audio_producer.id}"
                )
                self.emit(Events.REMOVE_PRODUCER, self.__audio_producer)

            @self.__audio_producer.observer.on("close")
            def on_producer_close():
                logger.debug(f"audio producer close :: {self.__audio_producer.id}")

            @self.__audio_producer.observer.on("pause")
            def on_producer_close():
                logger.debug(f"audio producer pause :: {self.__audio_producer.id}")

            @self.__audio_producer.observer.on("resume")
            def on_producer_close():
                logger.debug(f"audio producer resume :: {self.__audio_producer.id}")

            self.emit(Events.ADD_PRODUCER, self.__audio_producer)
            self.__producers.append(self.__audio_producer)
        except Exception as e:
            logger.error(f"MEDIA :: error while Enable Mic {e}")
            self.__emit_error("Not Able to Enable Mic")

    def disable_mic(self):
        logger.debug(f"RTC :: mic_enabled {self.mic_enabled}")
        if self.mic_enabled:
            self.__tasks[f"{TASK_ANY}-disable-mic"] = asyncio.ensure_future(
                self.async_disable_mic()
            )

    async def async_disable_mic(self):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "closeProducer",
                "data": {"producerId": self.__audio_producer.id},
            }
            await self.__audio_producer.close()
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)

            self.emit(Events.REMOVE_PRODUCER, self.__audio_producer)
            self.__producers.remove(self.__audio_producer)
            self.__audio_producer = None
        except Exception as e:
            logger.error(f"MEDIA :: error while Disable Mic {e}")
            self.__emit_error("Not Able to Disable Mic")

    def start_recording(self, recording_config: Optional[RecordingConfig] = None):
        logger.debug(f"RTC :: recording starting")
        self.__tasks[f"{TASK_ANY}-recording-start"] = asyncio.ensure_future(
            self.async_start_recording(recording_config)
        )

    async def async_start_recording(
        self, recording_config: Optional[RecordingConfig] = None
    ):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "startRecording",
                "data": {
                    "webhookUrl": recording_config.get("webhook_url", None),
                    "awsDirPath": recording_config.get("dir_path", None),
                    "config": recording_config.get("config", None),
                    "transcription": recording_config.get("transcription", None),
                },
            }
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)

        except Exception as e:
            logger.error(f"MEDIA :: error while start recording {e}")
            self.__emit_error("Not Able to Start Recording")

    def stop_recording(self):
        logger.debug(f"RTC :: recording stopping")
        self.__tasks[f"{TASK_ANY}-recording-stop"] = asyncio.ensure_future(
            self.async_stop_recording()
        )

    async def async_stop_recording(self):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "stopRecording",
                "data": None,
            }
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)

        except Exception as e:
            logger.error(f"MEDIA :: error while stop recording {e}")
            self.__emit_error("Not Able to stop Recording")

    def start_transcription(
        self, transcription_config: Optional[RecordingConfig] = None
    ):
        logger.debug(f"RTC :: transcription starting")
        self.__tasks[f"{TASK_ANY}-transcription-start"] = asyncio.ensure_future(
            self.async_start_transcription(transcription_config)
        )

    async def async_start_transcription(
        self, transcription_config: Optional[TranscriptionConfig] = None
    ):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "startTranscription",
                "data": {
                    "webhookUrl": transcription_config.get("webhook_url", None),
                    "summary": transcription_config.get("summary", None),
                },
            }
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)

        except Exception as e:
            logger.error(f"MEDIA :: error while start transcription {e}")
            self.__emit_error("Not Able to Start Transcription")

    def stop_transcription(self):
        logger.debug(f"RTC :: transcription starting")
        self.__tasks[f"{TASK_ANY}-transcription-stop"] = asyncio.ensure_future(
            self.async_stop_transcription()
        )

    async def async_stop_transcription(self):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "stopTranscription",
                "data": {},
            }
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)

        except Exception as e:
            logger.error(f"MEDIA :: error while start transcription {e}")
            self.__emit_error("Not Able to Start Transcription")

    def leave(self):
        """
        Leave the meeting room.
        """
        try:
            self.__tasks[f"{TASK_ANY}-leave"] = asyncio.ensure_future(
                self.async_leave()
            )
        except Exception as e:
            logger.error(f"Failed to Leave :: {e}")
            self.__emit_error("Failed to Leave from the Meeting")

    async def async_leave(self):
        """
        Asynchronously leave the meeting room.
        """
        try:
            self.emit(Events.MEETING_STATE_CHANGED, {"state": "CLOSING"})

            if self._closed:
                return
            self._closed = True

            if self.__websocket:
                logger.debug("closing websocket")
                await self.__websocket.close()

            if self.__send_transport:
                logger.debug("---> closing send transport connection")
                await self.__send_transport.close()
                if self.__audio_producer is not None:
                    await self.__audio_producer.close()
                if self.__video_producer is not None:
                    await self.__video_producer.close()

            if self.__recv_transport:
                logger.debug("---> closing recv transport connection")
                await self.__recv_transport.close()

            self.__clean()

            self.emit(Events.MEETING_STATE_CHANGED, {"state": "CLOSED"})
            self.emit(Events.MEETING_LEFT)

        except Exception as e:
            logger.error(f"Failed to Leave :: {e}")
            self.__emit_error("Failed to Leave from the Meeting")

    def close(self):
        """
        Close the RoomClient instance.
        """
        try:
            self.__tasks[f"{TASK_ANY}-close"] = asyncio.ensure_future(
                self.async_close()
            )
        except Exception as e:
            logger.error(f"Failed to Close :: {e}")
            self.__emit_error("Failed to Close the Meeting")

    async def async_close(self):
        """
        Asynchronously close the RoomClient instance.
        """
        try:
            if self.__websocket:
                reqId = self.__generate_random_number()
                req = {
                    "request": True,
                    "id": reqId,
                    "method": "closeRoom",
                    "data": {},
                }
                await self.__send_request(req)
                await self.__wait_for(self.__answers[reqId], timeout=15)
        except Exception as e:
            logger.error(f"Failed to Close :: {e}")
            self.__emit_error("Failed to Close the Meeting")

    def enable_peer_mic(self, peerId):
        self.__tasks[f"{TASK_ANY}-enable-peer-mic-{peerId}"] = asyncio.ensure_future(
            self.async_enable_peer_mic(peerId)
        )

    async def async_enable_peer_mic(self, peerId):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "enablePeerMic",
                "data": {"peerId": peerId},
            }
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)
        except Exception as e:
            logger.error(f"Signal :: Error while Enable Peer Mic :: {e}")
            self.__emit_error("Not Able to Enable Peer Mic")

    def disable_peer_mic(self, peerId):
        self.__tasks[f"{TASK_ANY}-disable-peer-mic-{peerId}"] = asyncio.ensure_future(
            self.async_disable_peer_mic(peerId)
        )

    async def async_disable_peer_mic(self, peerId):
        try:
            logger.debug(f"disable peer mic called {peerId}")
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "disablePeerMic",
                "data": {"peerId": peerId},
            }

            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)
        except Exception as e:
            logger.error(f"Signal :: Error while Disable Peer Mic :: {e}")
            self.__emit_error("Not Able to Disable Peer Mic")

    def enable_peer_webcam(self, peerId):
        self.__tasks[f"{TASK_ANY}-enable-peer-webcam-{peerId}"] = asyncio.ensure_future(
            self.async_enable_peer_webcam(peerId)
        )

    async def async_enable_peer_webcam(self, peerId):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "enablePeerWebcam",
                "data": {"peerId": peerId},
            }
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)
        except Exception as e:
            logger.error(f"Signal :: Error while Enable Peer Webcam :: {e}")
            self.__emit_error("Not Able to Enable Peer Webcam")

    def disable_peer_webcam(self, peerId):
        self.__tasks[f"{TASK_ANY}-disable-peer-webcam-{peerId}"] = (
            asyncio.ensure_future(self.async_disable_peer_webcam(peerId))
        )

    async def async_disable_peer_webcam(self, peerId):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "disablePeerWebcam",
                "data": {"peerId": peerId},
            }
            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)
        except Exception as e:
            logger.error(f"Signal :: Error while Disable Peer Webcam :: {e}")
            self.__emit_error("Not Able to Disable Peer Webcam")

    def remove_peer(self, peerId):
        self.__tasks[f"{TASK_ANY}-remove-peer"] = asyncio.ensure_future(
            self.async_remove_peer(peerId)
        )

    async def async_remove_peer(self, peerId):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "removePeer",
                "data": {"peerId": peerId},
            }

            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)
        except Exception as e:
            logger.error(f"Signal :: Error while Remove Peer :: {e}")
            self.__emit_error("Not Able to Remove Peer")

    def add_custom_video_track(self, track: MediaStreamTrack):
        self.__tasks[f"{TASK_ANY}-add-custom-video-track"] = asyncio.ensure_future(
            self.async_add_custom_video_track(track)
        )

    async def async_add_custom_video_track(self, track: MediaStreamTrack):
        try:
            logger.debug("custom video track adding...")
            await self.async_enable_webcam(custom_track=track)
            logger.debug("custom video track added")
            self.__video_stream_track = track
        except Exception as e:
            logger.error(f"RTC :: Error while Adding Custom Track :: {e}")
            self.__emit_error("Not Able to Add Custom Track")

    def add_custom_audio_track(self, track: MediaStreamTrack):
        self.__tasks[f"{TASK_ANY}-add-custom-audio-track"] = asyncio.ensure_future(
            self.async_add_custom_audio_track(track)
        )

    async def async_add_custom_audio_track(self, track: MediaStreamTrack):
        try:
            logger.debug("custom video track adding...")
            await self.async_enable_mic(custom_track=track)
            logger.debug("custom video track added")
            self.__video_stream_track = track
        except Exception as e:
            logger.error(f"RTC :: Error while Adding Custom Track :: {e}")
            self.__emit_error("Not Able to Add Custom Track")

    def handle_pubsub_publish(self, pubsub_config: PubSubPublishConfig):
        self.__tasks[f"{TASK_ANY}_publish"] = asyncio.ensure_future(
            self.async_handle_pubsub_publish(pubsub_config)
        )

    async def async_handle_pubsub_publish(self, pubsub_config: PubSubPublishConfig):
        try:
            options = pubsub_config.get("options", {})
            if "sendOnly" in options:
                send_only = options["sendOnly"]
                send_only_to_string = []
                if send_only:
                    for s in send_only:
                        if s:
                            send_only_to_string.append(str(s))
                options["sendOnly"] = send_only_to_string

            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "pubsubPublish",
                "data": {
                    "topic": pubsub_config.get("topic", None),
                    "message": pubsub_config.get("message", None),
                    "options": options,
                    "payload": pubsub_config.get("payload", None),
                },
            }

            await self.__send_request(req)
            await self.__wait_for(self.__answers[reqId], timeout=15)
        except Exception as e:
            logger.error(f"RTC :: Error while Publishing {e}", exc_info=True)
            self.__emit_error("Not Able to Publish")

    def handle_pubsub_subscribe(self, pubsub_config: PubSubSubscribeConfig):
        self.__tasks[f"{TASK_ANY}_publish"] = asyncio.ensure_future(
            self.async_handle_pubsub_subscribe(pubsub_config)
        )

    async def async_handle_pubsub_subscribe(self, pubsub_config: PubSubSubscribeConfig):
        try:
            reqId = self.__generate_random_number()
            req = {
                "request": True,
                "id": reqId,
                "method": "pubsubSubscribe",
                "data": {
                    "topic": pubsub_config.get("topic", None),
                },
            }

            await self.__send_request(req)
            ans = await self.__wait_for(self.__answers[reqId], timeout=15)
            return ans["data"]["messages"]
        except Exception as e:
            logger.error(f"RTC :: Error while Subscribe {e}", exc_info=True)
            self.__emit_error("Not Able to Subscribe")
            return None

    def release(self):
        self.__clean()

    def __clean(self):
        logger.debug("cleaning all running tasks")
        for task in self.__tasks.values():
            task.cancel()

    @property
    def attributes(self) -> Dict[str, Any]:
        return self.__attributes
