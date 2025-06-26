from fastapi import FastAPI, WebSocket, Query, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from dotenv import load_dotenv
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import os
import time
import asyncio
import psycopg
import psycopg.sql

# Load environment variables
load_dotenv()

app = FastAPI(title="Truck Tracker API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, replace with actual frontend URL
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve the UI
app.mount("/", StaticFiles(directory="static", html=True), name="static")

@app.get("/health")
def _health():
    return {"status": "ok"}

# Database connection parameters
# Use DATABASE_URL if available (Render best practice)
db_url = os.getenv("DATABASE_URL")
if db_url:
    db_params = db_url
else:
    db_params = {k: v for k, v in {
        "host": os.getenv("DB_HOST"),
        "port": os.getenv("DB_PORT"),
        "dbname": os.getenv("DB_NAME"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
    }.items() if v is not None}

STALE_THRESHOLD_HOURS = 1
IDLE_THRESHOLD_HOURS = 0.5

async def get_db_connection():
    try:
        if isinstance(db_params, str):
            conn = await psycopg.AsyncConnection.connect(db_params)
        else:
            # Only pass valid psycopg connection keys as strings
            valid_keys = {"host", "port", "dbname", "user", "password"}
            params = {k: str(v) for k, v in db_params.items() if k in valid_keys and v is not None}
            conn = await psycopg.AsyncConnection.connect(**params)
        return conn
    except Exception as e:
        print(f"Error connecting to database: {e}")
        raise HTTPException(status_code=500, detail=f"Database connection error: {str(e)}")

def determine_status(timestamp):
    current_time = int(time.time())
    time_diff_hours = (current_time - timestamp) / 3600
    if time_diff_hours > STALE_THRESHOLD_HOURS:
        return 'stopped'
    elif time_diff_hours > IDLE_THRESHOLD_HOURS:
        return 'idle'
    else:
        return 'moving'

async def get_truck_locations(
    car_filter: Optional[str] = None, 
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    try:
        conn = await get_db_connection()
        async with conn:
            async with conn.cursor() as cur:
                query = """
                WITH latest_records AS (
                    SELECT DISTINCT ON (car) 
                        car, 
                        latitude, 
                        longitude, 
                        "timestamp",
                        date
                    FROM 
                        public.tracking_data2
                    WHERE 
                        1=1
                """
                params = []
                if car_filter:
                    if ',' in car_filter:
                        vehicle_ids = [vid.strip() for vid in car_filter.split(',') if vid.strip()]
                        if vehicle_ids:
                            placeholders = ', '.join(['%s'] * len(vehicle_ids))
                            query += f" AND car IN ({placeholders})"
                            params.extend(vehicle_ids)
                    else:
                        query += " AND car = %s"
                        params.append(car_filter)
                if date_from:
                    query += " AND date >= %s"
                    params.append(date_from)
                if date_to:
                    query += " AND date <= %s"
                    params.append(date_to)
                query += """ 
                    ORDER BY 
                        car, 
                        "timestamp" DESC
                )
                SELECT * FROM latest_records
                """
                await cur.execute(query, params)
                rows = await cur.fetchall()
                features = []
                for row in rows:
                    # row is a tuple, so use index
                    car = row[0]
                    latitude = row[1]
                    longitude = row[2]
                    timestamp = row[3]
                    date = row[4]
                    status = determine_status(timestamp)
                    features.append({
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [
                                float(longitude),
                                float(latitude)
                            ]
                        },
                        "properties": {
                            "id": car,
                            "status": status,
                            "timestamp": timestamp,
                            "date": str(date),
                            "last_update": datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S')
                        }
                    })
                return features
    except Exception as e:
        print(f"Error getting truck locations: {e}")
        return []

@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    car_filter: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    await websocket.accept()
    try:
        while True:
            try:
                data = None
                try:
                    data = await asyncio.wait_for(websocket.receive_json(), timeout=0.01)
                    if data and 'filters' in data:
                        filters = data['filters']
                        car_filter = filters.get('car', car_filter)
                        date_from = filters.get('dateFrom', date_from)
                        date_to = filters.get('dateTo', date_to)
                        print(f"Received filters: vehicle={car_filter}, from={date_from}, to={date_to}")
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    print(f"Error processing client message: {e}")
                features = await get_truck_locations(car_filter, date_from, date_to)
                status_counts = {'moving': 0, 'idle': 0, 'stopped': 0}
                for feature in features:
                    status = feature['properties']['status']
                    status_counts[status] += 1
                print(f"Sending {len(features)} features to client")
                print(f"Status counts: {status_counts}")
                await websocket.send_json({
                    "type": "FeatureCollection",
                    "features": features,
                    "counts": status_counts
                })
                await asyncio.sleep(5)
            except Exception as e:
                print(f"Error in WebSocket loop: {e}")
                break
    finally:
        await websocket.close()

@app.get("/")
async def root():
    return {"message": "Truck Tracker API is running"}

@app.get("/api/trucks")
async def get_trucks(
    car: Optional[str] = Query(None, description="Filter by car number/name"),
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD)")
):
    features = await get_truck_locations(car, date_from, date_to)
    return {
        "type": "FeatureCollection",
        "features": features
    }

@app.get("/api/vehicles")
async def get_all_vehicles():
    try:
        print("Fetching all vehicle IDs...")
        conn = await get_db_connection()
        async with conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT DISTINCT car FROM public.tracking_data2")
                rows = await cur.fetchall()
                vehicles = [row[0] for row in rows]
                return {"vehicles": vehicles}
    except Exception as e:
        print(f"Error fetching vehicles: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/api/trucks/{car_id}")
async def get_truck_history(
    car_id: str,
    date_from: Optional[str] = Query(None, description="Filter from date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="Filter to date (YYYY-MM-DD)")
):
    try:
        conn = await get_db_connection()
        async with conn:
            async with conn.cursor() as cur:
                query = "SELECT * FROM public.tracking_data2 WHERE car = %s"
                params = [car_id]
                if date_from:
                    query += " AND date >= %s"
                    params.append(date_from)
                if date_to:
                    query += " AND date <= %s"
                    params.append(date_to)
                query += " ORDER BY \"timestamp\" DESC"
                await cur.execute(query, params)
                rows = await cur.fetchall()
                return {"history": rows}
    except Exception as e:
        print(f"Error fetching truck history: {e}")
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
