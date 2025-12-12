import asyncio
import google.generativeai as genai
from google.generativeai.types import GenerationConfig

from app.core.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

genai.configure(api_key=settings.GOOGLE_AI_STUDIO_KEY)

# Use a valid model name (e.g., gemini-1.5-flash or gemini-2.0-flash-exp)
# "gemini-2.5-flash" likely does not exist yet.
DEFAULT_MODEL = "models/gemini-flash-latest"
client = genai.GenerativeModel(DEFAULT_MODEL)


async def ask_gemini(prompt: str, max_tokens: int = 3000) -> str:
    try:
        # FIX 1: Use correct Google SDK method (generate_content_async)
        # FIX 2: Pass config for temperature/tokens via GenerationConfig
        resp = await client.generate_content_async(
            contents=prompt,
            generation_config=GenerationConfig(
                max_output_tokens=max_tokens, temperature=0.2
            ),
        )
        # FIX 3: Google response is accessed via .text, not .choices
        return resp.text
    except Exception as e:
        print(f"Error: {e}")
        return "Error generating response"


if __name__ == "__main__":
    # FIX 4: Define a main async function to run the coroutine
    async def main():
        user_question = "What is acs-cvrptw in one sentence?"
        # Now we can await!
        answer = await ask_gemini(user_question)
        print(f"Q: {user_question}")
        print(f"A: {answer}\n")

    # FIX 5: Use asyncio.run to execute the async main function
    asyncio.run(main())
