"""OKX wire constants: channel/op/state names, internal routing keys, signing
parameters, and order defaults. Endpoint URLs are deployment config (Config)."""

from __future__ import annotations

# keepalive: text sent, text received
PING_MESSAGE = "ping"
PONG_MESSAGE = "pong"

# routing keys used by our internal dispatch (ParsedMessage.key)
HEARTBEAT_KEY = "pong"
ERROR_KEY = "error"
SUBSCRIBE_ACK_KEY = "subscribe_ack"
IGNORED_KEY = "ignored"
LOGIN_KEY = "login"
ORDER_ACK_KEY = "order_ack"

# OKX channels
MARK_PRICE_CHANNEL = "mark-price"
FUNDING_RATE_CHANNEL = "funding-rate"
ORDERS_CHANNEL = "orders"

# OKX ops / event names
OP_SUBSCRIBE = "subscribe"
OP_LOGIN = "login"
OP_ORDER = "order"
OP_CANCEL_ORDER = "cancel-order"
EVENT_ERROR = "error"

# orders-channel order states
STATE_CANCELED = "canceled"

# private-channel login signing (timestamp + method + path, HMAC-SHA256, base64)
LOGIN_SIGN_METHOD = "GET"
LOGIN_SIGN_PATH = "/users/self/verify"

# order defaults
DEFAULT_TD_MODE = "isolated"
ORDER_TYPE_MARKET = "market"
INST_TYPE_SWAP = "SWAP"

OK_CODE = "0"
