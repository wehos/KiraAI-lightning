from core.provider import ModelType, BaseProvider
from .model_clients import GeminiLLMClient, GeminiEmbeddingClient, GeminiImageClient


class GeminiProvider(BaseProvider):
    models = {
        ModelType.LLM: GeminiLLMClient,
        ModelType.EMBEDDING: GeminiEmbeddingClient,
        ModelType.IMAGE: GeminiImageClient,
    }

    def __init__(self, provider_id, provider_name, provider_config):
        super().__init__(provider_id, provider_name, provider_config)
