# AUTOGENERATED! DO NOT EDIT! File to edit: ../nbs/000_FastKafkaAPI.ipynb.

# %% auto 0
__all__ = ['logger', 'FastKafkaAPI', 'produce_decorator', 'populate_consumers', 'populate_producers']

# %% ../nbs/000_FastKafkaAPI.ipynb 1
from typing import *
from typing import get_type_hints

from enum import Enum
from pathlib import Path
import json
import yaml
from copy import deepcopy
from os import environ
from datetime import datetime, timedelta
import tempfile
from contextlib import contextmanager, asynccontextmanager
import time
from inspect import signature
import functools

from fastcore.foundation import patch

import anyio
import asyncio
from asyncio import iscoroutinefunction  # do not use the version from inspect
import httpx
from fastapi import FastAPI
from fastapi import status, Depends, HTTPException, Request, Response
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydantic import Field, HttpUrl, EmailStr, PositiveInt
from pydantic.schema import schema
from pydantic.json import timedelta_isoformat
from aiokafka import AIOKafkaProducer

import confluent_kafka
from confluent_kafka import Producer
from confluent_kafka.admin import AdminClient, NewTopic
from confluent_kafka import Message, KafkaError
import asyncer

import fast_kafka_api._components.logger

fast_kafka_api._components.logger.should_supress_timestamps = True

import fast_kafka_api
from ._components.aiokafka_loop import aiokafka_consumer_loop
from fast_kafka_api._components.asyncapi import (
    KafkaMessage,
    export_async_spec,
    ConsumeCallable,
    ProduceCallable,
    KafkaBroker,
    ContactInfo,
    KafkaServiceInfo,
    KafkaBrokers,
)
from ._components.logger import get_logger, supress_timestamps

# %% ../nbs/000_FastKafkaAPI.ipynb 2
logger = get_logger(__name__)

# %% ../nbs/000_FastKafkaAPI.ipynb 7
class FastKafkaAPI(FastAPI):
    def __init__(
        self,
        *,
        title: str = "FastKafkaAPI",
        kafka_config: Dict[str, Any],
        contact: Optional[Dict[str, Union[str, Any]]] = None,
        kafka_brokers: Optional[Dict[str, Any]] = None,
        root_path: Optional[Union[Path, str]] = None,
        **kwargs,
    ):
        """Combined REST and Kafka service

        Params:
            title: name of the service, used for generating documentation
            kafka_config:
            contact:
            kafka_brokers:
            root_path:
            kwargs: parameters passed to FastAPI constructor
        """
        self._kafka_config = kafka_config

        config_defaults = {
            "bootstrap_servers": "localhost:9092",
            "auto_offset_reset": "earliest",
            "max_poll_records": 100,
            "max_buffer_size": 100,
        }

        for key, value in config_defaults.items():
            if key not in kafka_config:
                kafka_config[key] = value

        if root_path is None:
            root_path = Path(".")
        self._root_path = Path(root_path)

        if kafka_brokers is None:
            kafka_brokers = {
                "localhost": KafkaBroker(
                    url="https://localhost",
                    description="Local (dev) Kafka broker",
                    port="9092",
                )
            }
        if contact is None:
            contact = dict(
                name="author", url="https://www.google.com", email="noreply@gmail.com"
            )

        super().__init__(title=title, contact=contact, **kwargs)

        self._consumers_store: Dict[str, Tuple[ConsumeCallable, Dict[str, Any]]] = {}

        self._producers_store: Dict[
            str, Tuple[ProduceCallable, AIOKafkaProducer, Dict[str, Any]]
        ] = {}
        self._on_error_topic: Optional[str] = None

        contact_info = ContactInfo(**contact)  # type: ignore
        self._kafka_service_info = KafkaServiceInfo(
            title=self.title,
            version=self.version,
            description=self.description,
            contact=contact_info,
        )

        self._kafka_brokers = KafkaBrokers(brokers=kafka_brokers)

        self._asyncapi_path = self._root_path / "asyncapi"
        (self._asyncapi_path / "docs").mkdir(exist_ok=True, parents=True)
        (self._asyncapi_path / "spec").mkdir(exist_ok=True, parents=True)
        self.mount(
            "/asyncapi",
            StaticFiles(directory=self._asyncapi_path / "docs"),
            name="asyncapi",
        )

        self._is_shutting_down: bool = False
        self._kafka_consumer_tasks: List[asyncio.Task[Any]] = []
        self._kafka_producer_tasks: List[asyncio.Task[Any]] = []

        @self.get("/", include_in_schema=False)
        def redirect_root_to_asyncapi():
            return RedirectResponse("/asyncapi")

        @self.get("/asyncapi", include_in_schema=False)
        async def redirect_asyncapi_docs():
            return RedirectResponse("/asyncapi/index.html")

        @self.get("/asyncapi.yml", include_in_schema=False)
        async def download_asyncapi_yml():
            return FileResponse(self._asyncapi_path / "spec" / "asyncapi.yml")

        @self.on_event("startup")
        async def on_startup(app=self):
            await app._on_startup()

        @self.on_event("shutdown")
        async def on_shutdown(app=self):
            await app._on_shutdown()

    async def _on_startup(self) -> None:
        raise NotImplementedError

    async def _on_shutdown(self) -> None:
        raise NotImplementedError

    def consumes(
        self,
        topic: Optional[str] = None,
        *,
        prefix: str = "on_",
        **kwargs,
    ) -> ConsumeCallable:
        raise NotImplementedError

    def produces(
        self,
        topic: Optional[str] = None,
        *,
        prefix: str = "to_",
        producer: AIOKafkaProducer = None,
        **kwargs,
    ) -> ProduceCallable:
        raise NotImplementedError

# %% ../nbs/000_FastKafkaAPI.ipynb 8
def _get_topic_name(
    topic_callable: Union[ConsumeCallable, ProduceCallable], prefix: str = "on_"
) -> str:
    topic = topic_callable.__name__
    if not topic.startswith(prefix) or len(topic) <= len(prefix):
        raise ValueError(f"Function name '{topic}' must start with {prefix}")
    topic = topic[len(prefix) :]

    return topic

# %% ../nbs/000_FastKafkaAPI.ipynb 12
@patch
def consumes(
    self: FastKafkaAPI,
    topic: Optional[str] = None,
    *,
    prefix: str = "on_",
    **kwargs,
) -> ConsumeCallable:
    """Decorator registering the callback called when a message is received in a topic.

    This function decorator is also responsible for registering topics for AsyncAPI specificiation and documentation.

    Params:
        topic: Kafka topic that the consumer will subscribe to and execute the decorated function when it receives a message from the topic, default: None
            If the topic is not specified, topic name will be inferred from the decorated function name by stripping the defined prefix
        prefix: Prefix stripped from the decorated function to define a topic name if the topic argument is not passed, default: "on_"
            If the decorated function name is not prefixed with the defined prefix and topic argument is not passed, then this method will throw ValueError
        **kwargs: Keyword arguments that will be passed to AIOKafkaConsumer, used to configure the consumer

    Returns:
        A function returning the same function

    Throws:
        ValueError

    """

    def _decorator(
        on_topic: ConsumeCallable, topic: str = topic, kwargs=kwargs
    ) -> ConsumeCallable:
        if topic is None:
            topic = _get_topic_name(topic_callable=on_topic, prefix=prefix)

        self._consumers_store[topic] = (on_topic, kwargs)

        return on_topic

    return _decorator

# %% ../nbs/000_FastKafkaAPI.ipynb 14
def produce_decorator(self: FastKafkaAPI, func: ProduceCallable, topic: str):
    @functools.wraps(func)
    async def _produce(*args, **kwargs):
        return_val = func(*args, **kwargs)
        _, producer, _ = self._producers_store[topic]
        fut = await producer.send(topic, return_val.json().encode("utf-8"))
        msg = await fut
        return return_val

    return _produce

# %% ../nbs/000_FastKafkaAPI.ipynb 17
@patch
def produces(
    self: FastKafkaAPI,
    topic: Optional[str] = None,
    *,
    prefix: str = "to_",
    producer: AIOKafkaProducer = None,
    **kwargs,
) -> ProduceCallable:
    """Decorator registering the callback called when delivery report for a produced message is received

    This function decorator is also responsible for registering topics for AsyncAPI specificiation and documentation.

    Params:
        topic: Kafka topic that the producer will send returned values from the decorated function to, default: None
            If the topic is not specified, topic name will be inferred from the decorated function name by stripping the defined prefix
        prefix: Prefix stripped from the decorated function to define a topic name if the topic argument is not passed, default: "to_"
            If the decorated function name is not prefixed with the defined prefix and topic argument is not passed, then this method will throw ValueError
        producer:
        **kwargs: Keyword arguments that will be passed to AIOKafkaProducer, used to configure the producer

    Returns:
        A function returning the same function

    Throws:
        ValueError

    """

    def _decorator(
        on_topic: ProduceCallable, topic: str = topic, kwargs=kwargs
    ) -> ProduceCallable:
        if topic is None:
            topic = _get_topic_name(topic_callable=on_topic, prefix=prefix)

        self._producers_store[topic] = (on_topic, producer, kwargs)

        return produce_decorator(self, on_topic, topic)

    return _decorator

# %% ../nbs/000_FastKafkaAPI.ipynb 21
def populate_consumers(
    *,
    app: FastKafkaAPI,
    is_shutting_down_f: Callable[[], bool],
) -> List[asyncio.Task]:
    config: Dict[str, Any] = app._kafka_config
    tx = [
        asyncio.create_task(
            aiokafka_consumer_loop(
                topics=[topic],
                callbacks={topic: consumer},
                msg_types={topic: signature(consumer).parameters["msg"].annotation},
                is_shutting_down_f=is_shutting_down_f,
                **{**config, **configuration},
            )
        )
        for topic, (consumer, configuration) in app._consumers_store.items()
    ]

    return tx

# %% ../nbs/000_FastKafkaAPI.ipynb 22
# TODO: Add passing of vars


async def populate_producers(*, app: FastKafkaAPI) -> None:
    config: Dict[str, Any] = app._kafka_config
    for topic, (
        topic_callable,
        potential_producer,
        configuration,
    ) in app._producers_store.items():
        if type(potential_producer) != AIOKafkaProducer:
            producer = AIOKafkaProducer(
                **{"bootstrap_servers": config["bootstrap_servers"], **configuration}
            )
            asyncio.create_task(producer.start())
            app._producers_store[topic] = (topic_callable, producer, configuration)
        else:
            asyncio.create_task(potential_producer.start())

# %% ../nbs/000_FastKafkaAPI.ipynb 23
@patch
async def _on_startup(self: FastKafkaAPI) -> None:
    export_async_spec(
        consumers={topic: topic_callable for topic, (topic_callable, _) in self._consumers_store.items()},  # type: ignore
        producers={topic: topic_callable for topic, (topic_callable, _, _) in self._producers_store.items()},  # type: ignore
        kafka_brokers=self._kafka_brokers,
        kafka_service_info=self._kafka_service_info,
        asyncapi_path=self._asyncapi_path,
    )

    self._is_shutting_down = False

    def is_shutting_down_f(self: FastKafkaAPI = self) -> bool:
        return self._is_shutting_down

    self._kafka_consumer_tasks = populate_consumers(
        app=self,
        is_shutting_down_f=is_shutting_down_f,
    )

    await populate_producers(app=self)


@patch
async def _on_shutdown(self: FastKafkaAPI) -> None:
    self._is_shutting_down = True

    if self._kafka_consumer_tasks:
        await asyncio.wait(self._kafka_consumer_tasks)

    [await producer.stop() for _, producer, _ in self._producers_store.values()]

    self._is_shutting_down = False
