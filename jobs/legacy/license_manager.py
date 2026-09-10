import os
import uuid
import requests

class JaysNetworkLicense:
    def __init__(self, license_key):
        self.license_key = license_key
        self.gateway_url = "https://api.jays-network.com"
        self.gatekeeper_secret = os.getenv('GATEKEEPER_SECRET')
        
        if not self.gatekeeper_secret:
            print("⚠️ WARNING: GATEKEEPER_SECRET is missing!")

    def get_hwid(self):
        # 1. Check for a permanent static ID in the .env file
        static_id = os.getenv('STATIC_HWID')
        if static_id:
            return static_id
            
        # 2. Fallback to the shifting container IDs
        try:
            if os.path.exists('/etc/machine-id'):
                with open('/etc/machine-id', 'r') as f:
                    return f.read().strip()
            return str(uuid.getnode())
        except Exception:
             return "UNKNOWN_HWID"

    def authenticate(self):
        hwid = self.get_hwid()
        
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "JaysNetwork-Client/1.0",
            "X-Gatekeeper-Secret": self.gatekeeper_secret 
        }
        
        payload = {
            "license_key": self.license_key,
            "hwid": hwid
        }

        try:
            print(f"🔐 Handshaking with Gatekeeper (HWID: {hwid})...")
            
            response = requests.post(
                self.gateway_url, 
                json=payload, 
                headers=headers, 
                timeout=15
            )

            if response.status_code == 200:
                data = response.json()
                
                # Fetching Postgres details sent back from the Gatekeeper
                url = data.get("url")
                key = data.get("key")
                password = data.get("pass")

                if not url or not key or not password:
                    print(f"❌ Handshake Succeeded, but DB credentials missing.")
                    print(f"   Keys received: {list(data.keys())}")
                    return None
                
                print("✅ License Validated. Database Credentials Received.")
                return {"url": url, "key": key, "pass": password}

            elif response.status_code in [403, 405]:
                print(f"⛔ Firewall Rejection ({response.status_code})")
                return None
            else:
                print(f"❌ Validation Failed: {response.text}")
                return None
                
        except Exception as e:
            print(f"🔥 Connection Error: {e}")
            return None