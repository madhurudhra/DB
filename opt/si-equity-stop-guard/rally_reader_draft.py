import asyncio
import json
import re
import sys
from datetime import datetime, timezone
ANALYZER = '\nimport json\nimport sys\nsys.path.insert(0, "/opt/si-equity-r1")\nimport entry_signals_v1\nresult = entry_signals_v1.analyze(sys.argv[1])\nprint(json.dumps({\n    "state": result["state"],\n    "symbol": result["symbol"],\n    "score": result["technical_score"],\n    "candle_start": result["completed_candle_start"],\n}, allow_nan=False))\n'

async def run_process(argv, timeout=8):
    if timeout <= 0:
        raise ValueError('Invalid timeout')
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        if process.returncode != 0:
            raise RuntimeError('SCORER_PROCESS_FAILED')
        if len(stdout) > 16384:
            raise RuntimeError('SCORER_OUTPUT_TOO_LARGE')
        return json.loads(stdout)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()

def validate(data, symbol, now=None):
    if not isinstance(data, dict):
        raise ValueError('INVALID_SCORE_RESPONSE')
    if data.get('state') != 'TECHNICAL_DATA_READY' or data.get('symbol') != symbol:
        raise ValueError('SCORE_IDENTITY_OR_STATE_MISMATCH')
    score = data.get('score')
    if type(score) is not int or not 0 <= score <= 10:
        raise ValueError('INVALID_SCORE')
    stamp = datetime.fromisoformat(data['candle_start'].replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('MISSING_CANDLE_TIMEZONE')
    current = now or datetime.now(timezone.utc)
    end_age = (current - stamp).total_seconds() - 300
    if not 0 <= end_age <= 600:
        raise ValueError('STALE_OR_INCOMPLETE_CANDLE')
    return {'score': score, 'fresh': True}

async def fetch(symbol):
    if not isinstance(symbol, str) or not re.fullmatch('[A-Z][A-Z0-9.-]{0,9}', symbol):
        raise ValueError('INVALID_SYMBOL')
    data = await run_process([sys.executable, '-B', '-c', ANALYZER, symbol], timeout=8)
    return validate(data, symbol)
