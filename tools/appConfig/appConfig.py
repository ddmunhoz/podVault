import pydantic
import json
from typing import Annotated, Optional, List
from pathlib import Path
import json
import re
import os
os.environ['PYDANTIC_ERRORS_INCLUDE_URL'] = '0'

SCRIPT_DIR = Path(__file__).parent.parent.parent

class podcastEntry(pydantic.BaseModel):
    name: str
    monthsBack: Annotated[int, pydantic.Field(gt=0)] = 1
    url: pydantic.HttpUrl
    filter: bool = False  
    filter_Include: List[str] = []
    filter_Exclude: List[str] = []
    manual_only: bool = False 
    manual_episode_list: List[str] = []

class podcastConfig(pydantic.BaseModel):
    podcast: List[podcastEntry]

class appConfig(pydantic.BaseModel):
    @classmethod
    def load_and_validate(cls, config_path: Path) -> 'appConfig':
        """Loads and validates the JSON config file before creating the model."""
        try:
            with open(config_path) as f:
                try:
                    config_data = json.load(f)
                except json.JSONDecodeError as e:
                    raise ValueError(f"❌ Invalid JSON format in config file: {str(e)}")
                
                # Now try to create and validate the model
                return cls(**config_data)
        except FileNotFoundError:
            raise ValueError(f"❌ Config file not found at: {config_path}")

    urlFile: str
    notifySignal: bool = False
    notifyErrors: bool = True
    logLevel: Annotated[str, pydantic.Field(pattern=r'^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$')] = "INFO"
    checkInterval: Annotated[int, pydantic.Field(gt=0)] = 300  
    signalSender: str
    signalGroup: str
    signalEndpoint: pydantic.HttpUrl
    validated_podcast_data: Optional[podcastConfig] = None


    @pydantic.field_validator('logLevel', mode='before')
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        if isinstance(v, str):
            v = v.upper()
            if v not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
                raise ValueError(f'🔍 logLevel must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL not {v}')
            return v

    @pydantic.field_validator('urlFile')
    @classmethod
    def url_file_must_be_json_and_valid(cls, v: str) -> str:
        if not v.endswith('.json'):
            raise ValueError('urlFile must be a json file.')

        urlFilePath = f"{SCRIPT_DIR}{v}"
        # Load and parse the file
        try:
            with open(urlFilePath, 'r') as f:
                data = json.load(f)
        except FileNotFoundError:
            raise ValueError(f'urlFile not found at "{urlFilePath}"')
        except json.JSONDecodeError:
            raise ValueError(f'urlFile "{urlFilePath}" is not a valid JSON file.')
        
        validated_podcast_data = podcastConfig.model_validate(data)
        cls._podcast_config_data = validated_podcast_data
        cls.updateUrlFile(urlFilePath, validated_podcast_data)
        return urlFilePath
    
    @pydantic.field_validator('signalSender')
    @classmethod
    def signal_sender_must_be_valid(cls, v: str) -> str:
        base64_pattern = r'^\+[0-9]+$'

        if not re.match(base64_pattern, v):
            raise ValueError('signalSender must be a valid phone number in the format: +1234567890')

        return v

    @pydantic.field_validator('signalGroup')
    @classmethod
    def signal_group_must_be_valid(cls, v: str) -> str:
        base64_pattern = r'^group\.[a-zA-Z0-9+/=]+$'
        
        if not re.match(base64_pattern, v):
            raise ValueError('signalGroup must start with "group." and be followed by chars and numbers like: group.Vadf87098xjdf0-8E1EL28vUjl0987809867dkjLKJHljhfsd=')
            
        return v
    
    @classmethod
    def updateUrlFile(cls, urlFilePath: str, validated_data: podcastConfig):
        updated_data = validated_data.model_dump(
            mode='json',
            exclude_none=True,
        )

        with open(urlFilePath, 'w') as f:
            json.dump(updated_data, f, indent=4)
    
    def get_data(self) -> dict:
        full_config = self.model_dump()
        return full_config
    

