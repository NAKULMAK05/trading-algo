import logging
import os
import sys
from kiteconnect.exceptions import InputException, KiteException

# ensure repo root in path if needed
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from typing import Dict, Any, Optional, List
import requests
import hashlib
import pyotp
from dotenv import load_dotenv
from brokers.base import BrokerBase
from kiteconnect import KiteConnect, KiteTicker
import pandas as pd
from threading import Thread

from logger import logger

# load env vars (safe even if already loaded by server)
load_dotenv()


class ZerodhaBroker(BrokerBase):
    def __init__(self, without_totp: bool = True):
        """
        without_totp: when True, use kite.login_url() + request_token flow.
                      when False, attempt automated username/password + totp flow (not recommended).
        """
        super().__init__()
        self.without_totp = without_totp
        self.kite = None
        self.auth_response_data = None
        self.kite_ws = None
        self.tick_counter = 0
        self.symbols = []
        # Authenticate and setup kite + websocket only after success
        self._init_auth_and_ws()

    def _init_auth_and_ws(self):
        try:
            kite, resp = self.authenticate()
            self.kite = kite
            self.auth_response_data = resp or {}
            # extract access token safely
            access_token = None
            if isinstance(resp, dict):
                access_token = resp.get("access_token") or resp.get("data", {}).get("access_token")
            if not access_token:
                logger.error("Authentication succeeded but no access token found in response: %s", resp)
                raise Exception("No access token returned by generate_session()")

            # set access token on kite client
            try:
                self.kite.set_access_token(access_token)
            except Exception:
                # some KiteConnect versions do not require set_access_token or use different API; ignore if fails
                logger.debug("kite.set_access_token call failed or not required; continuing.")

            # Create kite websocket client now that access token exists
            try:
                api_key = os.getenv("BROKER_API_KEY")
                self.kite_ws = KiteTicker(api_key=api_key, access_token=access_token)
            except Exception as e:
                logger.warning("Failed to initialize KiteTicker websocket: %s", e)
                self.kite_ws = None

            logger.info("Broker authentication complete and websocket initialized (if available).")
        except Exception as e:
            logger.exception("Failed to initialize ZerodhaBroker: %s", e)
            # re-raise so caller knows initialization failed
            raise

    def authenticate(self):
        """
        Authenticate with Zerodha (Kite).
        - Uses BROKER_API_KEY and BROKER_API_SECRET from env.
        - If BROKER_REQUEST_TOKEN present in env, uses it automatically.
        - If without_totp is True and no BROKER_REQUEST_TOKEN, prints login URL and prompts for request token (interactive).
        - If without_totp is False, tries the automated login + TOTP flow (username/password + totp).
        Returns (kite_client, response_dict)
        """
        api_key = os.getenv("BROKER_API_KEY")
        api_secret = os.getenv("BROKER_API_SECRET")
        broker_id = os.getenv("BROKER_ID")
        totp_secret = os.getenv("BROKER_TOTP_KEY")
        password = os.getenv("BROKER_PASSWORD")
        # optional pre-supplied short-lived request token
        env_request_token = os.getenv("BROKER_REQUEST_TOKEN")

        if not api_key or not api_secret:
            logger.error("BROKER_API_KEY or BROKER_API_SECRET missing in environment. Please set them in .env or environment.")
            raise Exception("Missing BROKER_API_KEY / BROKER_API_SECRET")

        kite = KiteConnect(api_key=api_key)

        # If a request token is provided via env, use it automatically
        if env_request_token:
            request_token = env_request_token.strip()
            logger.info("Using BROKER_REQUEST_TOKEN from environment for auto-login.")
            try:
                resp = kite.generate_session(request_token, api_secret)
                logger.info("generate_session returned successfully using env request token.")
                return kite, resp
            except Exception as e:
                logger.exception("generate_session failed using BROKER_REQUEST_TOKEN from env: %s", e)
                raise

        # If without_totp (normal dev flow), prompt user (frontend) to paste the request token after logging in
        if self.without_totp:
            try:
                login_url = kite.login_url()
                # Print + log so frontend sees the instruction and URL
                print(f"Please Login to Zerodha and get the request token from the URL.\n{login_url}\nThen paste the request token here:")
                logger.info("Please login at: %s", login_url)

                # Use input() — this is the interactive prompt that will appear in the browser frontend terminal
                request_token = input("Request Token: ").strip()
                if not request_token:
                    logger.error("No request token entered by user.")
                    raise Exception("No request token provided.")

                try:
                    resp = kite.generate_session(request_token, api_secret)
                    logger.info("generate_session succeeded via interactive request token.")
                    return kite, resp
                except Exception as e:
                    logger.exception("generate_session failed using interactive request token: %s", e)
                    raise
            except Exception:
                raise

        # Else: attempt automated login + TOTP flow (non-interactive)
        # Requires BROKER_ID, BROKER_PASSWORD, BROKER_TOTP_KEY present
        if not all([broker_id, password, totp_secret]):
            logger.error("Automated TOTP login requested but missing BROKER_ID/BROKER_PASSWORD/BROKER_TOTP_KEY in env.")
            raise Exception("Missing credentials for TOTP flow.")

        session = requests.Session()

        try:
            # Step 1: Login
            login_url = "https://kite.zerodha.com/api/login"
            login_payload = {"user_id": broker_id, "password": password}
            login_resp = session.post(login_url, data=login_payload)
            login_data = login_resp.json()
            if not login_data.get("data"):
                logger.error("Login failed: %s", login_data)
                raise Exception(f"Login failed: {login_data}")
            request_id = login_data["data"]["request_id"]

            # Step 2: TwoFA
            twofa_url = "https://kite.zerodha.com/api/twofa"
            twofa_payload = {
                "user_id": broker_id,
                "request_id": request_id,
                "twofa_value": pyotp.TOTP(totp_secret).now(),
            }
            twofa_resp = session.post(twofa_url, data=twofa_payload)
            twofa_data = twofa_resp.json()
            if not twofa_data.get("data"):
                logger.error("2FA failed: %s", twofa_data)
                raise Exception(f"2FA failed: {twofa_data}")

            # Step 3: fetch connect URL and extract request_token
            connect_url = f"https://kite.trade/connect/login?api_key={api_key}"
            connect_resp = session.get(connect_url, allow_redirects=True)
            if "request_token=" not in connect_resp.url:
                logger.error("Failed to get request_token from redirect URL: %s", connect_resp.url)
                raise Exception("Failed to get request_token from redirect URL.")
            request_token = connect_resp.url.split("request_token=")[1].split("&")[0]

            # Generate session
            resp = kite.generate_session(request_token, api_secret)
            logger.info("generate_session succeeded via automated TOTP flow.")
            return kite, resp

        except Exception as e:
            logger.exception("Automated login flow failed: %s", e)
            raise

    # ---------------------------
    # Broker methods
    # ---------------------------
    def get_orders(self):
        return self.kite.orders()

    # unified get_quote: accept either "EXCHANGE:SYMBOL" or "SYMBOL" + exchange argument
    def get_quote(self, symbol, exchange=None):
        try:
            if exchange:
                if ":" not in symbol:
                    key = f"{exchange}:{symbol}"
                else:
                    key = symbol
            else:
                key = symbol
            return self.kite.quote(key)
        except Exception as e:
            logger.error("Error fetching quote for %s (%s): %s", symbol, exchange, e)
            raise

    def place_gtt_order(self, symbol, quantity, price, transaction_type, order_type, exchange, product, tag="Unknown"):
        if order_type not in ["LIMIT", "MARKET"]:
            raise ValueError(f"Invalid order type: {order_type}")
        if transaction_type not in ["BUY", "SELL"]:
            raise ValueError(f"Invalid transaction type: {transaction_type}")

        order_obj = {
            "exchange": exchange,
            "tradingsymbol": symbol,
            "transaction_type": transaction_type,
            "quantity": quantity,
            "order_type": order_type,
            "product": product,
            "price": price,
            "tag": tag,
        }
        last_price = self.get_quote(symbol, exchange)[f"{exchange}:{symbol}"]["last_price"]
        order_id = self.kite.place_gtt(
            trigger_type=self.kite.GTT_TYPE_SINGLE,
            tradingsymbol=symbol,
            exchange=exchange,
            trigger_values=[price],
            last_price=last_price,
            orders=order_obj,
        )
        return order_id["trigger_id"]

    def place_order(
        self,
        symbol,
        quantity,
        price,
        transaction_type,
        order_type,
        variety,
        exchange,
        product,
        tag="Unknown",
        trigger_price=None,
    ):
        valid_types = ["LIMIT", "MARKET", "SL", "SL-M"]
        if order_type not in valid_types:
            raise ValueError(f"Invalid order type: {order_type}")

        if order_type == "LIMIT":
            order_type_kite = "LIMIT"
        elif order_type == "MARKET":
            order_type_kite = "MARKET"
        elif order_type in ["SL", "STOPLOSS_LIMIT"]:
            order_type_kite = "SL"
        elif order_type in ["SL-M", "STOPLOSS_MARKET"]:
            order_type_kite = "SL-M"
        else:
            order_type_kite = order_type

        if variety == "REGULAR":
            variety_kite = self.kite.VARIETY_REGULAR
        else:
            raise ValueError(f"Invalid variety: {variety}")

        logger.info(
            "Placing order: %s | Qty=%s | Type=%s | Txn=%s | Price=%s | Trigger=%s | Product=%s | Variety=%s",
            symbol,
            quantity,
            order_type_kite,
            transaction_type,
            price,
            trigger_price,
            product,
            variety,
        )

        order_params = dict(
            variety=variety_kite,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=transaction_type,
            quantity=quantity,
            product=product,
            order_type=order_type_kite,
            tag=tag,
        )

        if order_type_kite == "LIMIT":
            order_params["price"] = price
        elif order_type_kite in ["SL", "SL-M"]:
            if trigger_price is None:
                raise ValueError("Stop-Loss order requires 'trigger_price'")
            order_params["trigger_price"] = trigger_price
            if order_type_kite == "SL":
                order_params["price"] = price or trigger_price

        try:
            order_id = self.kite.place_order(**order_params)
            logger.info("Order placed successfully: %s | ID=%s", symbol, order_id)
            return order_id
        except InputException as ie:
            logger.error("Order placement rejected by broker/exchange for %s: %s", symbol, ie)
            return -1
        except KiteException as ke:
            logger.error("Kite exception while placing order for %s: %s", symbol, ke)
            return -1
        except Exception as e:
            logger.error("Unexpected error placing order for %s: %s", symbol, e)
            return -1

    def get_positions(self):
        return self.kite.positions()

    def symbols_to_subscribe(self, symbols):
        self.symbols = symbols

    # Websocket callbacks
    def on_ticks(self, ws, ticks):  # noqa
        logger.info("Ticks: %s", ticks)

    def on_connect(self, ws, response):  # noqa
        logger.info("Connected")
        try:
            ws.subscribe(self.symbols)
            ws.set_mode(ws.MODE_FULL, self.symbols)
        except Exception as e:
            logger.error("Error subscribing to symbols on connect: %s", e)

    def on_order_update(self, ws, data):
        logger.info("Order update : %s", data)

    def on_close(self, ws, code, reason):
        logger.info("Connection closed: %s - %s", code, reason)

    def on_error(self, ws, code, reason):
        logger.info("Connection error: %s - %s", code, reason)

    def on_reconnect(self, ws, attempts_count):
        logger.info("Reconnecting: %s", attempts_count)

    def on_noreconnect(self, ws):
        logger.info("Reconnect failed.")

    def download_instruments(self):
        instruments = self.kite.instruments()
        self.instruments_df = pd.DataFrame(instruments)

    def get_instruments(self):
        return getattr(self, "instruments_df", None)

    def connect_websocket(self):
        if not self.kite_ws:
            logger.error("Websocket client not initialized; cannot connect.")
            return
        self.kite_ws.on_ticks = self.on_ticks
        self.kite_ws.on_connect = self.on_connect
        self.kite_ws.on_order_update = self.on_order_update
        self.kite_ws.on_close = self.on_close
        self.kite_ws.on_error = self.on_error
        self.kite_ws.on_reconnect = self.on_reconnect
        self.kite_ws.on_noreconnect = self.on_noreconnect
        # connect in a thread so it doesn't block main flow
        try:
            self.kite_ws.connect(threaded=True)
        except Exception as e:
            logger.error("Failed to start kite websocket: %s", e)
