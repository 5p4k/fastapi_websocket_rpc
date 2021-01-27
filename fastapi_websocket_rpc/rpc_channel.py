'''
Definition for an RPC channel protocol on top of a websocket - enabling bi-directional request/response interactions
'''
import asyncio
from inspect import _empty, getmembers, ismethod, signature
from typing import Any, Dict

from pydantic import ValidationError

from logging import getLogger
from .utils import gen_uid
from .rpc_methods import NoResponse, RpcMethodsBase
from .schemas import RpcMessage, RpcRequest, RpcResponse

from .logger import get_logger
logger = get_logger("RPC_CHANNEL")


class UnknownMethodException(Exception):
    pass


class RpcPromise:
    """
    Simple Event and id wrapper/proxy
    Holds the state of a pending request
    """

    def __init__(self, request: RpcRequest):
        self._request = request
        self._id = request.call_id
        # event used to wait for the completion of the request (upon receiving its matching response)
        self._event = asyncio.Event()

    @property
    def call_id(self):
        return self._id

    def set(self):
        """
        Signal compeltion of request with received response 
        """
        self._event.set()

    def wait(self):
        """
        Wait on the internal event - triggered on response  
        """
        return self._event.wait()


class RpcProxy:
    """
    Helper class
    provide a __call__ interface for an RPC method over a given channel
    """

    def __init__(self, channel, method_name) -> None:
        self.method_name = method_name
        self.channel = channel

    def __call__(self, **kwds: Any) -> Any:
        return self.channel.call(self.method_name, args=kwds)


class RpcCaller:
    """
    Helper class provide an object (aka other) with callable methods for each remote method on the otherside
    """

    def __init__(self, channel, methods=None) -> None:
        self._channel = channel
        self._method_names = [method[0] for method in getmembers(
            methods, lambda i: ismethod(i))] if methods is not None else None

    def __getattribute__(self, name: str):
        if not name.startswith("_") and (self._method_names is None or name in self._method_names):
            return RpcProxy(self._channel, name)
        else:
            return super().__getattribute__(name)


class RpcChannel:
    """
    A wire agnostic json-rpc channel protocol for both server and client.
    Enable each side to send RPC-requests (calling exposed methods on other side) and receive rpc-responses with the return value

    provides a .other property for callign remote methods.
    e.g. answer = channel.other.add(a=1,b=1) will (For example) ask the other side to perform 1+1 and will return an RPC-response of 2
    """

    def __init__(self, methods: RpcMethodsBase, socket, channel_id=None, **kwargs):
        """

        Args:
            methods (RpcMethodsBase): RPC methods to expose to other side
            socket: socket object providing simple send/recv methods
            channel_id (str, optional): uuid for channel. Defaults to None in which case a rnadom UUID is generated.
        """
        self.methods = methods.copy()
        # allow methods to access channel (for recursive calls - e.g. call me as a response for me calling you)
        self.methods.set_channel(self)
        # Pending requests - id-mapped to async-event
        self.requests: Dict[str, asyncio.Event] = {}
        # Received responses
        self.responses = {}
        self.socket = socket
        # Unique channel id
        self.id = channel_id if channel_id is not None else gen_uid()
        #
        # convineice caller
        # TODO - pass remote methods object to support validation before call
        self.other = RpcCaller(self)
        self._on_disconnect = None
        # any other kwarg goes straight to channel context (Accessible to methods)
        self._context = kwargs or {}

    @property
    def context(self) -> Dict[str, Any]:
        return self._context

    def get_return_type(self, method):
        method_signature = signature(method)
        return method_signature.return_annotation if method_signature.return_annotation is not _empty else str

    async def send(self, data):
        """
        For internal use. wrap calls to underlying socket
        """
        await self.socket.send(data)

    async def receive(self):
        """
        For internal use. wrap calls to underlying socket
        """
        return await self.socket.recv()

    async def on_message(self, data):
        """
        Handle an incoming RPC message
        This is the main function servers/clients using the channel need to call (upon reading a message on the wire)
        """
        try:
            message = RpcMessage.parse_raw(data)
            if message.request is not None:
                await self.on_request(message.request)
            if message.response is not None:
                await self.on_response(message.response)
        except ValidationError as e:
            logger.error(f"Failed to parse message", message=data, error=e)

    def register_disconnect_handler(self, coro):
        self._on_disconnect = coro

    async def on_disconnect(self):
        if self._on_disconnect is not None:
            return await self._on_disconnect(self.id)

    async def on_request(self, message: RpcRequest):
        """
        Handle incoming RPC requests - calling relevant exposed method

        Args:
            message (RpcRequest): the RPC request with the method to call
        """
        # TODO add exception support (catch exceptions and pass to other side as response with errors)
        logger.info("Handling RPC request", request=message.dict())
        method = getattr(self.methods, message.method)
        if callable(method):
            result = await method(**message.arguments)
            if result is not NoResponse:
                # get indicated type
                result_type = self.get_return_type(method)
                # if no type given - try to convert to string
                if result_type is str and type(result) is not str:
                    result = str(result)
                response = RpcMessage(response=RpcResponse[result_type](
                    call_id=message.call_id, result=result, result_type=result_type.__name__))
                await self.send(response.json())

    async def on_response(self, response: RpcResponse):
        """
        Handle an incoming response to a previous RPC call

        Args:
            response (RpcResponse): the received response
        """
        logger.info("Handling RPC response", response=response.dict())
        if response.call_id is not None and response.call_id in self.requests:
            self.responses[response.call_id] = response
            promise = self.requests[response.call_id]
            promise.set()

    async def wait_for_response(self, promise):
        """
        Wait on a previously made call
        """
        await promise.wait()
        response = self.responses[promise.call_id]
        del self.requests[promise.call_id]
        del self.responses[promise.call_id]
        return response

    async def async_call(self, name, args={}):
        """
        Call a method and return the event and the sent message (including the chosen call_id)
        use self.wait_for_response on the event and call_id to get the return value of the call
        """
        msg = RpcMessage(request=RpcRequest(
            method=name, arguments=args, call_id=gen_uid()))
        logger.info("Calling RPC method", message=msg.dict())
        await self.send(msg.json())
        promise = self.requests[msg.request.call_id] = RpcPromise(msg.request)
        return promise

    async def call(self, name, args={}):
        """
        Call a method and wait for a response to be received
        """
        promise = await self.async_call(name, args)
        return await self.wait_for_response(promise)