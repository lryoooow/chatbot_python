import type { ChatMessage, ChatRequestBody, ProviderConfigBody } from "../types";

export function buildChatRequestBody({
  messages,
  model,
  systemPrompt,
  stream,
  providerConfig,
  conversationId,
  useRag,
}: {
  messages: ChatMessage[];
  model: string;
  systemPrompt: string;
  stream: boolean;
  providerConfig: ProviderConfigBody;
  conversationId?: string | null;
  useRag: boolean;
}): ChatRequestBody {
  const body: ChatRequestBody = { messages, stream, use_memory: true, use_rag: useRag };
  if (model.trim()) body.model = model.trim();
  if (systemPrompt.trim()) body.system_prompt = systemPrompt.trim();
  if (Object.keys(providerConfig).length > 0) body.provider_config = providerConfig;
  if (conversationId) body.conversation_id = conversationId;
  return body;
}
