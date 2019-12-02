from __future__ import annotations

import enum
import sys
import traceback
from typing import Any, Final, Optional

import attr
try:
    import snappy  # type: ignore
    has_snappy: Final = True
except ImportError:
    has_snappy: Final = False  # type: ignore

from ..abc import (
    AbstractSerializer, AbstractDeserializer,
    AbstractMessage, RawHeaderBody,
)
from ..exceptions import ConfigurationError
from ..serialize import mpackb, munpackb


# TODO(FUTURE): zero-copy serialization and de-serialization


class TupleEncodingMixin:
    '''
    Encodes the class values in order into a msgpack tuple
    and decodes the object from such msgpack tuples.

    The class must be an attrs class.
    '''

    @classmethod
    def decode(cls, buffer: bytes) -> Any:
        if not buffer:
            return None
        return cls(*munpackb(buffer))

    def encode(self) -> bytes:
        cls = type(self)
        values = [getattr(self, f.name) for f in attr.fields(cls)]
        return mpackb(values)


class Metadata(TupleEncodingMixin, object):
    '''
    Base type for metadata.
    '''
    pass


@attr.dataclass(frozen=True, slots=True)
class FunctionMetadata(Metadata):
    pass


@attr.dataclass(frozen=True, slots=True)
class ResultMetadata(Metadata):
    pass


@attr.dataclass(frozen=True, slots=True)
class StreamMetadata(Metadata):
    resource_name: str
    length: int


@attr.dataclass(frozen=True, slots=True)
class ErrorMetadata(Metadata):
    name: str
    traceback: str


@attr.dataclass(frozen=True, slots=True)
class NullMetadata(Metadata):
    pass


class RPCMessageTypes(enum.IntEnum):
    FUNCTION = 0
    STREAM = 1
    RESULT = 2   # result of functions
    FAILURE = 3  # error from user handlers
    ERROR = 4    # error from callosum or underlying libraries
    CANCEL = 5   # client-side timeout or cancel request


# mapped from RPCMessageTypes as index
metadata_types = (
    FunctionMetadata,
    StreamMetadata,
    ResultMetadata,
    ErrorMetadata,
    ErrorMetadata,  # intended duplication
    NullMetadata,
)


@attr.dataclass(frozen=True, slots=True, auto_attribs=True)
class RPCMessage(AbstractMessage):
    # header parts
    msgtype: RPCMessageTypes
    method: str        # function/stream ID
    order_key: str  # replied back as-is
    seq_id: int      # replied back as-is

    # body parts (compressable)
    metadata: Optional[Metadata]
    body: Optional[bytes]

    @property
    def request_id(self):
        return (self.method, self.order_key, self.seq_id)

    @classmethod
    def result(cls, request, result_body):
        '''
        Creates an RPCMessage instance represents a execution result.
        '''
        return cls(
            RPCMessageTypes.RESULT,
            request.method, request.order_key, request.seq_id,
            ResultMetadata(),
            result_body,
        )

    @classmethod
    def failure(cls, request):
        '''
        Creates an RPCMessage instance containing exception information,
        when the exception is from user-defined handlers or upper adaptation layers.

        It must be called in an exception handler context, where ``sys.exc_info()``
        returns a non-null tuple.
        '''
        exc_info = sys.exc_info()
        return cls(
            RPCMessageTypes.FAILURE,
            request.method, request.order_key, request.seq_id,
            ErrorMetadata(exc_info[0].__name__, traceback.format_exc()),
            None,
        )

    @classmethod
    def error(cls, request):
        '''
        Creates an RPCMessage instance containing exception information,
        when the exception is from Callosum's internals.

        It must be called in an exception handler context, where ``sys.exc_info()``
        returns a non-null tuple.
        '''
        exc_info = sys.exc_info()
        return cls(
            RPCMessageTypes.ERROR,
            request.method, request.order_key, request.seq_id,
            ErrorMetadata(exc_info[0].__name__, traceback.format_exc()),
            None,
        )

    @classmethod
    def cancel(cls, request):
        '''
        Creates an RPCMessage instance represents a cancellation of
        the given request.
        '''
        return cls(
            RPCMessageTypes.CANCEL,
            request.method, request.order_key, request.seq_id,
            NullMetadata(), None,
        )

    @classmethod
    def decode(cls, raw_msg: RawHeaderBody,
               deserializer: AbstractDeserializer) -> RPCMessage:
        header = munpackb(raw_msg[0])
        msgtype = RPCMessageTypes(header['type'])
        compressed = header['zip']
        raw_data = raw_msg[1]
        if compressed:
            if not has_snappy:
                raise ConfigurationError('python-snappy is not installed')
            raw_data = snappy.decompress(raw_data)
        data = munpackb(raw_data)
        metadata = metadata_types[msgtype].decode(data['meta'])
        if msgtype in (RPCMessageTypes.FUNCTION, RPCMessageTypes.RESULT):
            body = deserializer(data['body'])
        else:
            body = data['body']
        return cls(msgtype,
                   header['meth'],
                   header['okey'],
                   header['seq'],
                   metadata,
                   body)

    def encode(self, serializer: AbstractSerializer, compress: bool = False) \
              -> RawHeaderBody:
        metadata = b''
        if self.metadata is not None:
            metadata = self.metadata.encode()
        header = {
            'type': int(self.msgtype),
            'meth': self.method,
            'okey': self.order_key,
            'seq': self.seq_id,
            'zip': compress,
        }
        serialized_header: bytes = mpackb(header)
        body: Optional[bytes]
        if self.msgtype in (RPCMessageTypes.FUNCTION, RPCMessageTypes.RESULT):
            body = serializer(self.body)
        else:
            body = self.body
        data = {
            'meta': metadata,
            'body': body,
        }
        serialized_data: bytes = mpackb(data)
        if compress:
            if not has_snappy:
                raise ConfigurationError('python-snappy is not installed')
            serialized_data = snappy.compress(serialized_data)
        return (serialized_header, serialized_data)
