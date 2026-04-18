import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import base64
import logging
from pprint import pformat

class signalBot:
    """Simple class to send messages using a Signal-compatible Bot API."""

    def __init__(self, sigSender: str, sigGroup: str, sigEndpoint: str, logger=None):
        self.sigSender = sigSender
        self.sigGroup = sigGroup
        self.sigEndpoint = sigEndpoint
        self.logger = logger or logging.getLogger(__name__)
        self.session = requests.Session()
        
        retry_strategy = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
    
    def sendMessage(self, payload: dict = None, bot_message: str = None, silently: bool = False, type: str = 'text', binPayload: str = None):
        """
        Send a message or a payload dict dynamically.
        - You can pass either a raw message string via bot_message,
          or a payload dict with multiple fields.
        - If payload includes 'image_url', fetch and base64 encode image automatically.
        """

        if payload:
            # Check for image URL in payload
            image_url = payload.get('image_url')
            if image_url:
                try:
                    resp = self.session.get(image_url, timeout=30)
                    resp.raise_for_status()
                    binPayload = base64.b64encode(resp.content).decode('utf-8')
                    type = 'image'
                    payload.pop('image_url', None)  # Remove image_url from text message
                except Exception as e:
                    self.logger.warning(f"Failed to fetch image {image_url}: {e}")
                    type = 'text'
                    binPayload = None

            # Build dynamic message from payload
            lines = []
            for key, value in payload.items():
                if isinstance(value, (dict, list)):
                    value_str = pformat(value)
                else:
                    value_str = str(value)
                key_str = key.replace('_', ' ').capitalize()
                lines.append(f"{key_str}: {value_str}")
            bot_message = "\n".join(lines) + "\n"

        if not bot_message:
            bot_message = "No message content provided."

        try:
            data = {
                "message": bot_message,
                "number": self.sigSender,
                "recipients": [self.sigGroup]
            }

            if type == 'image' and binPayload:
                data["base64_attachments"] = [binPayload]

            response = self.session.post(f'{self.sigEndpoint}/v2/send', json=data, timeout=10)
            response.raise_for_status()
            return response.json()

        except requests.RequestException as e:
            self.logger.error(f"Signal message send failed: {e}")
            return None
