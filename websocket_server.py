import asyncio
import json
import pymysql
import websockets
from datetime import datetime, timedelta
from decimal import Decimal
import os
from dotenv import load_dotenv

load_dotenv()  # Load .env file

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASS", ""),
    "database": os.getenv("DB_NAME", "aprs_db"),
    "port": int(os.getenv("DB_PORT", 3306))
}

connected_clients = set()

def json_serializer(obj):
    if isinstance(obj, datetime):
        # Split date and time
        date_str = obj.strftime('%d-%m-%Y')
        time_str = obj.strftime('%H:%M:%S')
        return f"{date_str} {time_str}"
    raise TypeError(f"Type {obj.__class__.__name__} not serializable")

async def fetch_unique_senders_data():
    """Fetch latest data for each unique sender that exists in the information table"""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT p1.sender, p1.latitude, p1.longitude, p1.time_received, p1.message, p1.place,
                        p1.battery_percentage,
                        i.name, i.address, i.phone_number, i.boat_color, i.engine_type, i.boat_length

                FROM aprs_packets p1
                INNER JOIN (
                    SELECT sender, MAX(time_received) AS max_time
                    FROM aprs_packets
                    GROUP BY sender
                ) p2 ON p1.sender = p2.sender AND p1.time_received = p2.max_time
                INNER JOIN information i ON p1.sender = i.callsign  -- ✅ Compare sender vs callsign
            """)

            data = cursor.fetchall()

        connection.close()

        unique_senders = set(entry["sender"] for entry in data)
        unique_count = len(unique_senders)
        now = datetime.now()

        for entry in data:
            entry["latitude"] = float(entry["latitude"])
            entry["longitude"] = float(entry["longitude"])

            # ✅ Safely convert time_received to datetime if it’s a string
            last_time_str = entry["time_received"]
            if isinstance(last_time_str, str):
                try:
                    last_time = datetime.strptime(last_time_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    # fallback for other timestamp formats
                    last_time = datetime.fromisoformat(last_time_str.split('.')[0])
            else:
                last_time = last_time_str

            # ✅ Calculate time gap safely
            gap_minutes = (now - last_time).total_seconds() / 60
            entry["time_gap_minutes"] = gap_minutes
            entry["is_delayed"] = gap_minutes > 20
            entry["time_received"] = last_time.strftime("%B %d, %Y, %I:%M %p")
            entry["message"] = entry.get("message", "")
            entry["place"] = entry.get("place", "")
            entry["battery_percentage"] = entry.get("battery_percentage", None)

        return {"type": "update", "stations": data, "count": unique_count}

    except Exception as e:
        print(f"Database error: {e}")
        return {"type": "error", "message": "Database connection failed"}




async def fetch_sender_trail(sender):
    """Fetch last 20 locations of a specific sender that exists in the information table"""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            cursor.execute("""
            SELECT p.sender, p.latitude, p.longitude, p.time_received, p.message, p.place,
                p.battery_percentage,
                i.name, i.address, i.boat_color, i.engine_type, i.boat_length
            FROM aprs_packets p
            INNER JOIN information i ON p.sender = i.callsign
            WHERE p.sender = %s
            ORDER BY p.time_received DESC
            LIMIT 20
        """, (sender,))

            data = cursor.fetchall()

        connection.close()

        for entry in data:
            entry["latitude"] = float(entry["latitude"])
            entry["longitude"] = float(entry["longitude"])
            entry["time_received"] = entry["time_received"].strftime("%Y-%m-%d %H:%M:%S")
            entry["message"] = entry.get("message", "")
            entry["place"] = entry.get("place", "")
            entry["battery_percentage"] = entry.get("battery_percentage", None)


        return {"type": "search_result", "sender": sender, "locations": data}

    except Exception as e:
        print(f"Database error: {e}")
        return {"type": "error", "message": "Failed to retrieve trail"}



# ✅ Optimized broadcast: only updates when database changes
async def broadcast_latest_updates():
    last_count = 0
    while True:
        try:
            connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS cnt FROM aprs_packets")
                current_count = cursor.fetchone()["cnt"]

            # Only broadcast if new packets exist
            if current_count != last_count:
                update_data = await fetch_unique_senders_data()
                message = json.dumps(update_data)
                if connected_clients:
                    await asyncio.gather(*[client.send(message) for client in connected_clients])
                last_count = current_count

            connection.close()
        except Exception as e:
            print("⚠️ broadcast_latest_updates error:", e)

        await asyncio.sleep(3)  # Check every 3 seconds



async def mark_sender_safe(sender):
    """Insert a new 'Marked as Safe' record only if sender is registered."""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            # ✅ Verify if sender exists in information table
            cursor.execute("SELECT callsign FROM information WHERE callsign = %s", (sender,))
            if not cursor.fetchone():
                print(f"❌ Sender '{sender}' not found in information table.")
                return False

            # ✅ Get the latest APRS record for this sender
            cursor.execute("""
                SELECT sender, latitude, longitude, place
                FROM aprs_packets
                WHERE sender = %s
                ORDER BY time_received DESC
                LIMIT 1
            """, (sender,))
            latest = cursor.fetchone()

            if not latest:
                print(f"No previous record found for sender {sender}")
                return False

            # ✅ Insert new record with updated message
            cursor.execute("""
                INSERT INTO aprs_packets (sender, latitude, longitude, time_received, message, place)
                VALUES (%s, %s, %s, NOW(), 'Marked as Safe', %s)
            """, (latest["sender"], latest["latitude"], latest["longitude"], latest["place"]))

            connection.commit()
            print(f"✅ Sender '{sender}' marked as safe.")
            return True

    except Exception as e:
        print(f"Database error during mark_safe: {e}")
        return False
    finally:
        if connection:
            connection.close()



async def mark_sender_help(sender):
    """Insert a new 'Help on the Way' record only if sender is registered."""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            # ✅ Verify if sender exists in information table
            cursor.execute("SELECT callsign FROM information WHERE callsign = %s", (sender,))
            if not cursor.fetchone():
                print(f"❌ Sender '{sender}' not found in information table.")
                return False

            # ✅ Get the latest APRS record for this sender
            cursor.execute("""
                SELECT sender, latitude, longitude, place
                FROM aprs_packets
                WHERE sender = %s
                ORDER BY time_received DESC
                LIMIT 1
            """, (sender,))
            latest = cursor.fetchone()

            if not latest:
                print(f"No previous record found for sender {sender}")
                return False

            # ✅ Insert a new row with updated message
            cursor.execute("""
                INSERT INTO aprs_packets (sender, latitude, longitude, time_received, message, place)
                VALUES (%s, %s, %s, NOW(), 'Help on the Way', %s)
            """, (latest["sender"], latest["latitude"], latest["longitude"], latest["place"]))

            connection.commit()
            print(f"✅ Help on the Way message added for '{sender}'.")
            return True

    except Exception as e:
        print(f"Database error during mark_help_on_way: {e}")
        return False
    finally:
        if connection:
            connection.close()


async def mark_sender_not_found(sender):
    """Mark sender as 'Not Found' only if registered."""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            # ✅ Verify if sender exists in information table
            cursor.execute("SELECT callsign FROM information WHERE callsign = %s", (sender,))
            if not cursor.fetchone():
                print(f"❌ Sender '{sender}' not found in information table.")
                return False

            # ✅ Update only the latest APRS record for this sender
            cursor.execute("""
                UPDATE aprs_packets
                SET message = 'Not Found'
                WHERE id = (
                    SELECT id FROM (
                        SELECT id FROM aprs_packets
                        WHERE sender = %s
                        ORDER BY time_received DESC
                        LIMIT 1
                    ) AS subquery
                )
            """, (sender,))
            connection.commit()

            if cursor.rowcount > 0:
                print(f"⚠️ Sender '{sender}' marked as Not Found.")
                return True
            else:
                print(f"No record found to mark as Not Found for sender '{sender}'.")
                return False

    except Exception as e:
        print(f"Database error during mark_not_found: {e}")
        return False
    finally:
        if connection:
            connection.close()





async def handle_connection(websocket):
    """Handle WebSocket client connections"""
    connected_clients.add(websocket)
    print(f"New client connected ({len(connected_clients)} total)")

    try:
        while True:
            message = await websocket.recv()
            data = json.loads(message)

            if data["type"] == "search":  # Fetch sender trail
                sender_data = await fetch_sender_trail(data["sender"])
                await websocket.send(json.dumps(sender_data))

            elif data["type"] == "mark_safe":  # Mark sender as safe
                success = await mark_sender_safe(data["sender"])
                if success:
                    update_data = await fetch_unique_senders_data()
                    # ✅ Broadcast to all clients, not just the sender
                    if connected_clients:
                        await asyncio.gather(*[client.send(json.dumps(update_data)) for client in connected_clients])
                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Failed to mark sender as safe"
                    }))


            elif data["type"] == "help_on_way":  # Mark sender for help
                success = await mark_sender_help(data["sender"])
                if success:
                    update_data = await fetch_unique_senders_data()
                    # ✅ Broadcast to all connected clients
                    if connected_clients:
                        await asyncio.gather(*[client.send(json.dumps(update_data)) for client in connected_clients])
                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Failed to update sender status"
                    }))


            elif data["type"] == "fetch_history":  # Fetch sender history
                sender = data["sender"]
                start_date = data.get("start_date", None)
                end_date = data.get("end_date", None)
                sender_data = await fetch_sender_history(sender, start_date, end_date)
                await websocket.send(json.dumps(sender_data))

            elif data["type"] == "safe_report":  # Fetch Safe Report
                await fetch_safe_report(websocket)

            elif data["type"] == "fetch_information":  # Fetch Information Table
                info_data = await fetch_information_data()
                await websocket.send(json.dumps(info_data))

            elif data["type"] == "not_found":  # Mark sender as Not Found
                success = await mark_sender_not_found(data["sender"])
                if success:
                    update_data = await fetch_unique_senders_data()
                    # ✅ Broadcast to all clients
                    if connected_clients:
                        await asyncio.gather(*[client.send(json.dumps(update_data)) for client in connected_clients])
                else:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Failed to mark sender as Not Found"
                    }))


            elif data["type"] == "sender_report":  # Fetch Sender Report
                await fetch_sender_report(websocket)

            elif data["type"] == "fetch_sender_details":
                sender = data["sender"]
                month = data["month"]
                details_data = await fetch_sender_details(sender, month)


                # ✅ Send with datetime serialization
                await websocket.send(json.dumps(details_data, default=json_serializer))





    finally:
        connected_clients.remove(websocket)
        print(f"Client disconnected ({len(connected_clients)} remain)")


async def fetch_information_data():
    """Fetch all records from the information table"""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            cursor.execute("""
            SELECT id, name, callsign, address, phone_number, boat_color, engine_type, boat_length 
            FROM information
            """)
            data = cursor.fetchall()

        connection.close()

        return {"type": "information_data", "info": data}

    except Exception as e:
        print(f"Database error: {e}")
        return {"type": "error", "message": "Failed to retrieve information data"}



async def fetch_safe_report(websocket):
    """Count SOS, Safe, Help, Not Found (unique per sender per day) grouped by month, only for verified senders."""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+08:00'")  # ✅ Force Philippine time zone

            cursor.execute("""
                SELECT 
                    DATE_FORMAT(t.time_received, '%M-%Y') AS month,
                    SUM(CASE WHEN t.message = 'SOS' THEN 1 ELSE 0 END) AS sos_count,
                    SUM(CASE WHEN t.message = 'Marked as Safe' THEN 1 ELSE 0 END) AS safe_count,
                    SUM(CASE WHEN t.message = 'Help on the Way' THEN 1 ELSE 0 END) AS help_count,
                    SUM(CASE WHEN t.message = 'Not Found' THEN 1 ELSE 0 END) AS not_found_count
                FROM (
                    SELECT p.sender, p.message, DATE(p.time_received) AS day, MAX(p.time_received) AS time_received
                    FROM aprs_packets p
                    INNER JOIN information i ON p.sender = i.callsign  -- ✅ Only verified senders
                    WHERE p.message IN ('SOS', 'Marked as Safe', 'Help on the Way', 'Not Found')
                    GROUP BY p.sender, DATE(p.time_received), p.message
                ) AS t
                GROUP BY DATE_FORMAT(t.time_received, '%M-%Y')
                ORDER BY MAX(t.time_received) DESC;
            """)
            data = cursor.fetchall()

        # Convert Decimal → int for JSON
        for row in data:
            for key, value in row.items():
                if isinstance(value, Decimal):
                    row[key] = int(value)

        await websocket.send(json.dumps({
            "type": "safe_report",
            "report": data
        }))
        print("📊 Sent monthly safe report (verified senders only).")

    except Exception as e:
        import traceback
        traceback.print_exc()
        await websocket.send(json.dumps({
            "type": "error",
            "message": f"Failed to generate safe report: {str(e)}"
        }))
    finally:
        if connection:
            connection.close()



async def fetch_sender_report(websocket):
    """Count SOS, Safe, Help, Not Found per sender per month, only for verified senders."""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+08:00'")  # ✅ Fix for UTC mismatch

            cursor.execute("""
                SELECT 
                    DATE_FORMAT(t.time_received, '%M-%Y') AS month,
                    t.sender,
                    SUM(CASE WHEN t.message = 'SOS' THEN 1 ELSE 0 END) AS sos_count,
                    SUM(CASE WHEN t.message = 'Marked as Safe' THEN 1 ELSE 0 END) AS safe_count,
                    SUM(CASE WHEN t.message = 'Help on the Way' THEN 1 ELSE 0 END) AS help_count,
                    SUM(CASE WHEN t.message = 'Not Found' THEN 1 ELSE 0 END) AS not_found_count
                FROM (
                    SELECT p.sender, p.message, DATE(p.time_received) AS day, MAX(p.time_received) AS time_received
                    FROM aprs_packets p
                    INNER JOIN information i ON p.sender = i.callsign  -- ✅ Only verified senders
                    WHERE p.message IN ('SOS', 'Marked as Safe', 'Help on the Way', 'Not Found')
                    GROUP BY p.sender, DATE(p.time_received), p.message
                ) AS t
                GROUP BY DATE_FORMAT(t.time_received, '%M-%Y'), t.sender
                ORDER BY MAX(t.time_received) DESC, t.sender ASC;
            """)
            data = cursor.fetchall()

        # Convert Decimal → int
        for row in data:
            for key, value in row.items():
                if isinstance(value, Decimal):
                    row[key] = int(value)

        await websocket.send(json.dumps({
            "type": "sender_report",
            "report": data
        }))
        print("📊 Sent sender report (verified senders only).")

    except Exception as e:
        import traceback
        traceback.print_exc()
        await websocket.send(json.dumps({
            "type": "error",
            "message": f"Failed to generate sender report: {str(e)}"
        }))
    finally:
        if connection:
            connection.close()








async def fetch_sender_details(sender, month):
    """Fetch all SOS, Safe, Help, and Not Found messages for a sender in a given month."""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+08:00'")  # ✅ Ensure Manila time zone for month filter

            cursor.execute("""
                SELECT time_received, message
                FROM aprs_packets
                WHERE sender = %s
                  AND DATE_FORMAT(time_received, '%%M-%%Y') = %s
                  AND message IN ('SOS', 'Marked as Safe', 'Help on the Way', 'Not Found')
                ORDER BY time_received DESC
            """, (sender, month))

            data = cursor.fetchall()

        connection.close()
        return {"type": "sender_details", "details": data}

    except Exception as e:
        print(f"Database error: {e}")
        return {"type": "error", "message": "Failed to retrieve sender details"}







async def fetch_sender_history(sender, start_date=None, end_date=None):
    """Fetch messages for a sender within a specific date & time range (only if registered in information)"""
    try:
        connection = pymysql.connect(**DB_CONFIG, cursorclass=pymysql.cursors.DictCursor)
        with connection.cursor() as cursor:
            cursor.execute("SET time_zone = '+08:00'")

            sql_query = """
                SELECT p.sender, p.latitude, p.longitude, p.time_received, p.message, p.place,
                p.battery_percentage,
                i.name, i.address, i.boat_color, i.engine_type, i.boat_length

                FROM aprs_packets p
                INNER JOIN information i ON p.sender = i.callsign
                WHERE p.sender = %s
            """
            query_params = [sender]

            if start_date:
                sql_query += " AND p.time_received >= %s"
                query_params.append(start_date)

            if end_date:
                sql_query += " AND p.time_received <= %s"
                query_params.append(end_date)

            sql_query += " ORDER BY p.time_received DESC"

            cursor.execute(sql_query, query_params)
            data = cursor.fetchall()

        connection.close()

        for entry in data:
            entry["latitude"] = float(entry["latitude"])
            entry["longitude"] = float(entry["longitude"])

            # ✅ Safely handle both string and datetime types
            time_val = entry["time_received"]
            if isinstance(time_val, str):
                try:
                    time_obj = datetime.strptime(time_val, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    time_obj = datetime.fromisoformat(time_val.split('.')[0])
            else:
                time_obj = time_val

            entry["time_received"] = time_obj.strftime("%B %d, %Y, %I:%M %p")
            entry["message"] = entry.get("message", "")
            entry["place"] = entry.get("place", "")
            entry["battery_percentage"] = entry.get("battery_percentage", None)

        return {"type": "history_result", "sender": sender, "history": data}

    except Exception as e:
        print(f"Database error: {e}")
        return {"type": "error", "message": "Failed to retrieve sender history"}






async def start_server():
    """Start WebSocket server"""
    async with websockets.serve(handle_connection, "0.0.0.0", 8766):
        print("✅ WebSocket server started on ws://0.0.0.0:8766")

        await broadcast_latest_updates()


if __name__ == "__main__":
    asyncio.run(start_server())
