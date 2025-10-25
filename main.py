from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import re
import requests

#Setup
load_dotenv()

AFRO_TOKEN = os.getenv("AFRO_TOKEN")
AFRO_SENDER_ID = os.getenv("AFRO_SENDER_ID", "Tolo ET")
MONGO_URI = os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
PORT = int(os.getenv("PORT", 3000))

app = Flask(__name__, static_folder="build", static_url_path="/frontend/dist")
CORS(app, supports_credentials=True)
socketio = SocketIO(app, cors_allowed_origins="*")

client = MongoClient(MONGO_URI)
db = client["tolo_delivery"]
deliveries_col = db["deliveries"]
feedback_col = db["feedback"]


drivers_ = client["drivers"]
drivers_col = drivers_["drivers"]



#Helpers
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_react(path):
    if path != "" and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, "index.html")
def is_valid_ethiopian_number(phone: str) -> bool:
    if not phone:
        return False
    return re.fullmatch(r"(\+251|0)9\d{8}", str(phone)) is not None


def to_dt(val):
    if isinstance(val, datetime):
        return val
    try:
        return datetime.fromisoformat(val)
    except Exception:
        return None


def serialize_delivery(doc):
    if not doc:
        return None
    
    doc["_id"] = str(doc["_id"])
    
    # Timestamp as ISO string
    ts = doc.get("timestamp")
    if isinstance(ts, datetime):
        doc["timestamp"] = ts.isoformat()
    
    # Default values
    doc.setdefault("assigned_driver_id", None)
    doc.setdefault("assigned_driver_name", "Not Assigned")
    doc.setdefault("assigned_driver_phone", "")
    doc.setdefault("price", 0)
    doc.setdefault("delivery_type", "payable")
    doc.setdefault("status", "pending")
    
    # Fetch driver name and phone if assigned_driver_id exists
    if doc.get("assigned_driver_id") and not doc.get("assigned_driver_name"):
        driver = drivers_col.find_one({"_id": ObjectId(doc["assigned_driver_id"])})
        if driver:
            doc["assigned_driver_name"] = driver.get("name", "Unknown")
            doc["assigned_driver_phone"] = driver.get("phone", "")
    
    return doc


def serialize_driver(doc):
    doc["_id"] = str(doc["_id"])
    return doc


def emit_stats():
    stats = get_stats_payload()
    socketio.emit("stats_updated", stats, broadcast=True)


def get_stats_payload():
    total = deliveries_col.count_documents({})
    successful = deliveries_col.count_documents({"status": "successful"})
    unsuccessful = deliveries_col.count_documents({"status": "unsuccessful"})
    pending = deliveries_col.count_documents({"status": {"$in": [None, "", "pending"]}})
    feedback_count = feedback_col.count_documents({})
    return {
        "totalDeliveries": total,
        "successfulDeliveries": successful,
        "unsuccessfulDeliveries": unsuccessful,
        "pendingDeliveries": pending,
        "feedbackCount": feedback_count,
    }


def send_sms(phone_number, message):
    session = requests.Session()
    base_url = 'https://api.afromessage.com/api/send'
    headers = {
        'Authorization': f'Bearer {AFRO_TOKEN}',
        'Content-Type': 'application/json'
    }
    body = {
        'callback': 'YOUR_CALLBACK',
        'from': AFRO_SENDER_ID,
        'sender': 'Tolo ET',
        'to': phone_number,
        'message': message
    }
    result = session.post(base_url, json=body, headers=headers)
    if result.status_code == 200 and result.json().get('acknowledge') == 'success':
        return True
    return False


# Deliveries API
@app.get("/api/deliveries")
def get_deliveries():
    try:
        status = request.args.get("status")
        query = {}
        if status:
            if status == "pending":
                query["status"] = {"$in": [None, "", "pending"]}
            else:
                query["status"] = status

        deliveries = list(deliveries_col.find(query).sort("timestamp", -1))
        drivers = list(drivers_col.find())
        driver_map = {str(d["_id"]): d.get("name", "Unknown") for d in drivers}

        result = []
        for d in deliveries:
            delivery = serialize_delivery(d)
            driver_id = delivery.get("assigned_driver_id")
            delivery["assigned_driver_name"] = driver_map.get(driver_id, "Not Assigned")
            result.append(delivery)

        return jsonify(result)
    
    except Exception as e:
        print("❌ Error fetching deliveries:", e)
        return jsonify([])



from datetime import datetime
import pytz
from flask import request, jsonify

@app.route("/api/add_delivery", methods=["POST"])
def add_delivery():
    data = request.get_json()

    # Basic validation
    required_fields = ["pickup", "dropoff", "sender_phone", "receiver_phone"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"success": False, "error": f"{field} is required"}), 400

    # Define Addis Ababa timezone timestamp
    addis_tz = pytz.timezone("Africa/Addis_Ababa")
    now_addis = datetime.now(addis_tz)

    # Set default values if missing
    delivery = {
        "source": data.get("source", "web"),
        "user_name": data.get("user_name", "N/A"),
        "pickup": data["pickup"],
        "dropoff": data["dropoff"],
        "sender_phone": data["sender_phone"],
        "receiver_phone": data["receiver_phone"],
        "full_address": data.get("full_address", ""),
        "Quantity": data.get("Quantity", 0),  
        "item_description": data.get("item_description", ""),
        "price": data.get("price", None),
        "delivery_type": data.get("delivery_type", "payable"),
        "payment_from_sender_or_receiver": data.get("payment_from_sender_or_receiver", ""),  
        "driver_id": None,
        "status": "pending",
        "notified": False,
        "timestamp": now_addis.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        result = deliveries_col.insert_one(delivery)
        delivery["_id"] = str(result.inserted_id)
        return jsonify({"success": True, "delivery": delivery}), 201
    except Exception as e:
        print("Error adding delivery:", e)
        return jsonify({"success": False, "error": "Database error"}), 500


@app.patch("/api/deliveries/<delivery_id>")
def update_delivery(delivery_id):
    try:
        data = request.get_json(force=True) or {}
        # Normalize fields
        if "timestamp" in data:
            dt = to_dt(data["timestamp"]) or datetime.utcnow()
            data["timestamp"] = dt
        if "assigned_driver_id" in data and data["assigned_driver_id"]:
            # enrich with driver name/phone
            driver = drivers_col.find_one({"_id": ObjectId(data["assigned_driver_id"])})
            if driver:
                data["assigned_driver_name"] = driver.get("name")
                data["assigned_driver_phone"] = driver.get("phone")
        if "price" in data and data["price"] not in (None, ""):
            try:
                data["price"] = int(data["price"])
            except Exception:
                pass

        res = deliveries_col.update_one({"_id": ObjectId(delivery_id)}, {"$set": data})
        if res.matched_count == 0:
            return jsonify({"error": "Delivery not found"}), 404

        doc = serialize_delivery(deliveries_col.find_one({"_id": ObjectId(delivery_id)}))
        socketio.emit("delivery_updated", doc, broadcast=True)
        emit_stats()
        return jsonify(doc)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/delete_delivery/<delivery_id>", methods=["DELETE"])
def delete_delivery(delivery_id):
    try:
        result = deliveries_col.delete_one({"_id": ObjectId(delivery_id)})
        if result.deleted_count == 0:
            return jsonify({"success": False, "error": "Delivery not found"}), 404
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500



@app.post("/api/deliveries/<delivery_id>/status")
def set_status(delivery_id):
    body = request.get_json(force=True) or {}
    new_status = body.get("status")
    if new_status not in {"successful", "unsuccessful", "pending"}:
        return jsonify({"error": "Invalid status"}), 400

    deliveries_col.update_one({"_id": ObjectId(delivery_id)}, {"$set": {"status": new_status}})
    doc = serialize_delivery(deliveries_col.find_one({"_id": ObjectId(delivery_id)}))
    socketio.emit("delivery_updated", doc, broadcast=True)

    # Auto-SMS on successful status (optional)
    if new_status == "successful":
        sender_phone = doc.get("sender_phone")
        if is_valid_ethiopian_number(sender_phone):
            send_sms(sender_phone, "Dear Customer, your delivery is delivered successfully. Thank you for choosing Tolo Delivery.")

    emit_stats()
    return jsonify(doc)


# --- Driver APIs --------------------------------------------------------------

@app.get("/api/drivers")
def get_drivers():
    drivers = [serialize_driver(d) for d in drivers_col.find().sort("name", 1)]
    return jsonify(drivers)

from flask import request, jsonify
@app.post("/api/drivers")
def create_driver():
    body = request.get_json(force=True) or {}
    name = body.get("name")
    phone = body.get("phone")
    vehicle_plate = body.get("vehicle_plate")

    if not name:
        return jsonify({"error": "name required"}), 400

    drv = {"name": name, "phone": phone, "vehicle_plate": vehicle_plate}
    r = drivers_col.insert_one(drv)
    doc = serialize_driver(drivers_col.find_one({"_id": r.inserted_id}))

    # SocketIO emit removed
    # socketio.emit("driver_created", doc, broadcast=True)

    return jsonify(doc), 201


@app.post("/api/deliveries/<delivery_id>/assign_driver")
def assign_driver(delivery_id):
    body = request.get_json(force=True) or {}
    driver_id = body.get("driver_id")
    price = body.get("price")
    delivery_type = body.get("delivery_type")  # "payable" or "free"

    if not driver_id:
        return jsonify({"error": "driver_id required"}), 400

    driver = drivers_col.find_one({"_id": ObjectId(driver_id)})
    if not driver:
        return jsonify({"error": "Driver not found"}), 404

    update = {
        "assigned_driver_id": str(driver["_id"]),
        "assigned_driver_name": driver.get("name", "Unknown"),
        "assigned_driver_phone": driver.get("phone", ""),
    }

    # Optional: price
    if price is not None:
        try:
            update["price"] = int(price)
        except Exception:
            update["price"] = price  # fallback if not convertible

    # Optional: delivery type
    if delivery_type:
        update["delivery_type"] = delivery_type

    # Update in MongoDB
    deliveries_col.update_one(
        {"_id": ObjectId(delivery_id)},
        {"$set": update}
    )

    doc = serialize_delivery(deliveries_col.find_one({"_id": ObjectId(delivery_id)}))

    # Notify frontend via sockets
    socketio.emit("delivery_updated", doc, broadcast=True)
    socketio.emit("driver_assigned", {"delivery_id": delivery_id, **update}, broadcast=True)

    return jsonify(doc)


from flask import request, jsonify
from bson.objectid import ObjectId

@app.route("/api/notify_driver", methods=["POST"])
def notify_driver():
    try:
        data = request.get_json()
        delivery_id = data.get("delivery_id")

        if not delivery_id:
            return jsonify({"success": False, "error": "Missing delivery_id"}), 400

        delivery = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
        if not delivery or not delivery.get("assigned_driver_id"):
            return jsonify({"success": False, "error": "No driver assigned"}), 400

        driver = drivers_col.find_one({"_id": ObjectId(delivery["assigned_driver_id"])})

        if not driver or not driver.get("phone"):
            return jsonify({"success": False, "error": "Driver phone number not found"}), 400

        pickup_location = delivery.get("pickup", "N/A")
        dropoff_location = delivery.get("dropoff", "N/A")
        senderphone = delivery.get("sender_phone", "N/A")
        reciverphone = delivery.get("receiver_phone", "N/A")
        item = delivery.get("item_description", "N/A")
        price = delivery.get("price", "N/A")
        collect_from = delivery.get("payment_from_sender_or_receiver", "N/A")

        
        message_driver = (
            f"🚚 New Delivery Order\n"
            f"Pickup (Sender): {pickup_location}({senderphone})\n"
            f"Drop (Receiver): {dropoff_location}({reciverphone})\n"
            f"Items: {item} ({price})\n"
            f"Payment Collect from: {collect_from}\n"

        )

        message_customer = (
            f"📦 New Delivery Order / አዲስ ትዕዛዝ\n"
            f"Location: {pickup_location} to {dropoff_location}\n"
            f"Items: {item} ({price})\n"
            f"Driver: {driver.get('name', 'N/A')} - {driver.get('phone', 'N/A')} ({driver.get('vehicle_plate', 'N/A')})\n"
            "Tolo Delivery"
        )

        send_sms(driver.get("phone"), message_driver)
        send_sms(senderphone, message_customer)
        send_sms(reciverphone, message_customer)

        # Mark as notified
        deliveries_col.update_one(
            {"_id": ObjectId(delivery_id)}, {"$set": {"notified": True}}
        )

        return jsonify({"success": True, "message": "Driver and customer notified successfully"})

    except Exception as e:
        print("❌ Error sending SMS:", e)
        return jsonify({"success": False, "error": str(e)}), 500

from flask import request, jsonify
from bson.objectid import ObjectId

@app.route("/api/update_delivery_status/<delivery_id>", methods=["POST"])
def update_delivery_status(delivery_id):
    data = request.get_json(force=True) or {}

    # Include 'reason' for unsuccessful deliveries and match driver field name
    allowed_fields = ["status", "price", "delivery_type", "assigned_driver_id", "reason"]

    update_fields = {f: data[f] for f in allowed_fields if f in data}

    if not update_fields:
        return jsonify({"error": "No valid fields to update"}), 400

    try:
        deliveries_col.update_one(
            {"_id": ObjectId(delivery_id)},
            {"$set": update_fields}
        )

        doc = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
        if doc:
            doc["_id"] = str(doc["_id"])
            return jsonify({"success": True, "delivery": doc})
        else:
            return jsonify({"error": "Delivery not found"}), 404

    except Exception as e:
        print("Error updating delivery:", e)
        return jsonify({"error": "Internal server error"}), 500

@app.route("/deliveries/<delivery_id>/status", methods=["PUT"])
def update_delivery_status_api(delivery_id):
    data = request.get_json()
    status = data.get("status")
    reason = data.get("reason", "")

    if not status:
        return jsonify({"error": "Status is required"}), 400

    deliveries_col.update_one(
        {"_id": ObjectId(delivery_id)},
        {"$set": {"status": status, "reason": reason}}
    )
    return jsonify({"success": True})



from flask import Flask, request, jsonify
from bson.objectid import ObjectId

@app.route("/api/assign_driver", methods=["POST"])
def assign_driver_api():
    try:
        data = request.get_json()
        delivery_id = data.get("delivery_id")
        driver_id = data.get("driver_id")

        if not delivery_id or not driver_id:
            return jsonify({"success": False, "error": "Missing delivery_id or driver_id"}), 400

        # Fetch delivery and driver
        delivery = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
        driver = drivers_col.find_one({"_id": ObjectId(driver_id)})

        if not delivery or not driver:
            return jsonify({"success": False, "error": "Invalid delivery or driver"}), 400

        # Update delivery with assigned driver
        deliveries_col.update_one(
            {"_id": ObjectId(delivery_id)},
            {"$set": {
                "assigned_driver_id": str(driver["_id"]),
                "assigned_driver_name": driver["name"],
                "assigned_driver_phone": driver.get("phone", "")
            }}
        )


        # Return updated delivery info for frontend
        updated_delivery = deliveries_col.find_one({"_id": ObjectId(delivery_id)})
        updated_delivery["_id"] = str(updated_delivery["_id"])  # convert ObjectId to string

        return jsonify({"success": True, "delivery": updated_delivery}), 200

    except Exception as e:
        print("❌ Error in assigning driver:", e)
        return jsonify({"success": False, "error": str(e)}), 500

from flask import request, jsonify
from geopy.geocoders import Nominatim
from geopy.distance import geodesic

@app.route("/api/calculate_price", methods=["POST"])
def calculate_price():
    data = request.get_json()
    pickup = data.get("pickup")
    dropoff = data.get("dropoff")

    if not pickup or not dropoff:
        return jsonify({"error": "Missing pickup/dropoff"}), 400

    geolocator = Nominatim(user_agent="tolo_delivery")
    try:
        loc1 = geolocator.geocode(pickup)
        loc2 = geolocator.geocode(dropoff)
        if not loc1 or not loc2:
            return jsonify({"error": "Unable to geocode"}), 400

        distance_km = geodesic(
            (loc1.latitude, loc1.longitude), (loc2.latitude, loc2.longitude)
        ).km

        # --- your pricing logic ---
        base_fare = 50
        per_km_rate = 10
        price = int(base_fare + per_km_rate * distance_km)

        return jsonify({
            "price": price,
            "pickup_coords": [loc1.latitude, loc1.longitude],
            "dropoff_coords": [loc2.latitude, loc2.longitude],
            "distance_km": round(distance_km, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Update a driver
@app.put("/api/drivers/<driver_id>")
def update_driver(driver_id):
    data = request.get_json(force=True) or {}
    update_fields = {k: v for k, v in data.items() if k in ["name", "phone", "vehicle_plate"]}
    if not update_fields:
        return jsonify({"error": "No valid fields to update"}), 400
    try:
        result = drivers_col.update_one({"_id": ObjectId(driver_id)}, {"$set": update_fields})
        if result.matched_count == 0:
            return jsonify({"error": "Driver not found"}), 404
        driver = drivers_col.find_one({"_id": ObjectId(driver_id)})
        driver["_id"] = str(driver["_id"])
        # Removed socketio.emit
        return jsonify(driver)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# Delete a driver
@app.route("/api/drivers/<driver_id>", methods=["DELETE"])
def delete_driver(driver_id):
    try:
        result = drivers_col.delete_one({"_id": ObjectId(driver_id)})
        if result.deleted_count == 0:
            return jsonify({"error": "Driver not found"}), 404

        # Removed socketio.emit
        return jsonify({"message": "Deleted successfully"}), 200

    except Exception as e:
        print("❌ Error deleting driver:", e)
        return jsonify({"error": str(e)}), 500

# --- Feedback & Stats ---------------------------------------------------------

@app.get("/api/feedback")
def get_feedback():
    items = []
    for fb in feedback_col.find().sort("_id", -1):
        fb["_id"] = str(fb["_id"])
        items.append(fb)
    return jsonify(items)


@app.get("/api/statistics")
def get_statistics():
    return jsonify(get_stats_payload())


# --- Socket.IO lifecycle ------------------------------------------------------

@socketio.on("connect")
def on_connect():
    # Send initial stats snapshot on connect
    socketio.emit("stats_updated", get_stats_payload())


@socketio.on("disconnect")
def on_disconnect():
    pass




# --- Entrypoint ---------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    socketio.run(app, host="0.0.0.0", port=port)
