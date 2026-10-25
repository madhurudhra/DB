from fastapi import FastAPI
from app.api.status import router
app = FastAPI(title='Super Intelligence', version='0.1.0', docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(router)
from app.api.trading_controls import router as trading_controls_router
app.include_router(trading_controls_router)
from app.api.bot_configuration import router as bot_configuration_router
app.include_router(bot_configuration_router)
from app.api.dashboard_data_v1 import router as dashboard_data_v1_router
app.include_router(dashboard_data_v1_router)
from app.api.capital_api_v1 import router as capital_api_v1_router
app.include_router(capital_api_v1_router)
