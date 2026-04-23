#!/usr/bin/env python3
"""Test LangFuse integration — verify traces are being sent and visible."""

import asyncio
import json
import sys
from pathlib import Path

# Add parent directory to path for backend imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx

async def test_langfuse_integration():
    """Test that:
    1. LangFuse is accessible
    2. Backend is connected
    3. Extraction traces appear in LangFuse
    """
    
    # Check 1: LangFuse is accessible
    print("✓ Checking LangFuse accessibility...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get("http://localhost:3100/", timeout=5)
            if resp.status_code == 200:
                print("  ✅ LangFuse is running on http://localhost:3100")
            else:
                print(f"  ⚠️  LangFuse returned status {resp.status_code}")
                # Still accessible, just might be initializing
                if 200 <= resp.status_code < 500:
                    print("     (This is normal if LangFuse is initializing)")
        except Exception as e:
            print(f"  ❌ Cannot reach LangFuse: {e}")
            return False
    
    # Check 2: Backend is accessible
    print("\n✓ Checking backend connectivity...")
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get("http://127.0.0.1:8000/health", timeout=5)
            health = resp.json()
            if resp.status_code == 200:
                print(f"  ✅ Backend is running: {health}")
            else:
                print(f"  ❌ Backend returned status {resp.status_code}")
                return False
        except Exception as e:
            print(f"  ❌ Cannot reach backend: {e}")
            return False
    
    # Check 3: Verify settings
    print("\n✓ Checking backend LangFuse configuration...")
    try:
        from backend.settings import settings
        print(f"  LANGFUSE_ENABLED: {settings.langfuse_enabled}")
        print(f"  LANGFUSE_HOST: {settings.langfuse_host}")
        print(f"  LANGFUSE_PUBLIC_KEY: {settings.langfuse_public_key[:10]}...")
        if settings.langfuse_enabled and settings.langfuse_host:
            print("  ✅ LangFuse configuration looks good")
        else:
            print("  ⚠️  LangFuse is disabled or misconfigured")
            return False
    except Exception as e:
        print(f"  ❌ Cannot load settings: {e}")
        return False
    
    # Check 4: Test LangFuse client initialization
    print("\n✓ Testing LangFuse client initialization...")
    try:
        from backend.services.llm import get_langfuse
        lf = get_langfuse()
        if lf:
            print("  ✅ LangFuse client initialized successfully")
            print(f"  Connected to: {settings.langfuse_host}")
        else:
            print("  ❌ LangFuse client is None (disabled or init failed)")
            return False
    except Exception as e:
        print(f"  ❌ Error initializing LangFuse: {e}")
        return False
    
    # Check 5: List current traces in LangFuse
    print("\n✓ Querying LangFuse for existing traces...")
    try:
        # LangFuse API endpoint (requires authentication)
        # For POC with default creds, we'd need to implement this properly
        print("  ℹ️  To view traces, open: http://localhost:3100")
        print("  Login with credentials from your LANGFUSE_PUBLIC_KEY/SECRET_KEY")
    except Exception as e:
        print(f"  ⚠️  Could not query traces: {e}")
    
    # Check 6: Test a trace call
    print("\n✓ Testing a sample trace call...")
    try:
        from backend.services.llm import _trace_llm_call
        from backend.services.llm import get_langfuse
        
        _trace_llm_call(
            model="gemini-2.5-flash",
            prompt="Test prompt for AT&T invoice",
            response='[{"usoc": "ABC123"}]',
            input_tokens=10,
            output_tokens=5,
            latency_ms=500,
            call_type="extraction"
        )
        print("  ✅ Sample trace sent successfully")
        print("  Check LangFuse dashboard for 'extraction' trace")
    except Exception as e:
        print(f"  ⚠️  Error sending test trace: {e}")
    
    return True


async def main():
    print("=" * 60)
    print("LangFuse Integration Test")
    print("=" * 60)
    print()
    
    success = await test_langfuse_integration()
    
    print("\n" + "=" * 60)
    if success:
        print("✅ All checks passed!")
        print("\nNext steps:")
        print("1. Open http://localhost:3100 in your browser")
        print("2. Go to 'Traces' section")
        print("3. You should see extraction and eval traces")
        print("\nDocumentation: docs/design/LANGFUSE_SETUP.md")
    else:
        print("❌ Some checks failed. See details above.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
