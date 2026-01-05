#!/usr/bin/env python3
"""
Real-Debrid API Debug Script for Riven
This script helps debug Real-Debrid API connectivity and data retrieval issues
specifically for the hash that's causing problems: A754E2E9DC1F61F259F0FEA688BCEE1858DFA013
"""

import json
import requests
import sys
import os
from datetime import datetime
from typing import Optional, Dict, List, Any

class RealDebridDebugger:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://api.real-debrid.com/rest/1.0"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Riven-RD-Debugger/1.0"
        })
        
    def test_api_connection(self) -> bool:
        """Test basic API connectivity"""
        print("🔌 Testing Real-Debrid API connection...")
        try:
            response = self.session.get(f"{self.base_url}/user", timeout=30)
            response.raise_for_status()
            
            user_data = response.json()
            print(f"✅ Connected successfully")
            print(f"   User: {user_data.get('username', 'Unknown')}")
            print(f"   Premium: {user_data.get('premium', False)}")
            print(f"   Points: {user_data.get('points', 0)}")
            print(f"   Expiration: {user_data.get('expiration', 'Unknown')}")
            return True
        except Exception as e:
            print(f"❌ API connection failed: {e}")
            return False
    
    def get_all_torrents(self) -> List[Dict[str, Any]]:
        """Get all torrents from account with detailed logging"""
        print("\n📋 Fetching all torrents from Real-Debrid account...")
        try:
            response = self.session.get(f"{self.base_url}/torrents", timeout=30)
            response.raise_for_status()
            
            torrents = response.json()
            print(f"✅ Found {len(torrents)} total torrents")
            
            # Group by status
            status_count = {}
            for torrent in torrents:
                status = torrent.get('status', 'unknown')
                status_count[status] = status_count.get(status, 0) + 1
            
            print("📊 Torrent status breakdown:")
            for status, count in status_count.items():
                print(f"   {status}: {count}")
            
            return torrents
        except Exception as e:
            print(f"❌ Failed to fetch torrents: {e}")
            return []
    
    def search_for_hash(self, target_hash: str, torrents: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Search for specific hash with detailed comparison logging"""
        print(f"\n🔍 Searching for hash: {target_hash}")
        search_hash = target_hash.lower().strip()
        print(f"   Normalized search hash: {search_hash}")
        
        matches = []
        partial_matches = []
        
        for i, torrent in enumerate(torrents):
            torrent_hash = torrent.get('hash', '').lower().strip()
            
            # Exact match
            if torrent_hash == search_hash:
                matches.append(torrent)
                print(f"✅ EXACT MATCH found at index {i}:")
                print(f"   Torrent ID: {torrent.get('id')}")
                print(f"   Name: {torrent.get('filename', 'Unknown')}")
                print(f"   Status: {torrent.get('status', 'Unknown')}")
                print(f"   Hash: {torrent_hash}")
                print(f"   Added: {torrent.get('added', 'Unknown')}")
            
            # Partial match (in case of encoding issues)
            elif search_hash in torrent_hash or torrent_hash in search_hash:
                partial_matches.append(torrent)
                print(f"⚠️ PARTIAL MATCH found at index {i}:")
                print(f"   Torrent hash: {torrent_hash}")
                print(f"   Search hash:  {search_hash}")
        
        if not matches and not partial_matches:
            print(f"❌ No matches found for {target_hash}")
            print(f"   Searched through {len(torrents)} torrents")
            
            # Show a few example hashes for comparison
            print("\n📝 First 5 torrent hashes for comparison:")
            for i, torrent in enumerate(torrents[:5]):
                print(f"   {i+1}. {torrent.get('hash', 'No hash')}")
        
        return matches[0] if matches else None
    
    def analyze_torrent_details(self, torrent_id: str, infohash: str) -> Optional[Dict[str, Any]]:
        """Get detailed torrent information"""
        print(f"\n🔬 Analyzing torrent details for ID: {torrent_id}")
        try:
            response = self.session.get(f"{self.base_url}/torrents/info/{torrent_id}", timeout=30)
            response.raise_for_status()
            
            torrent_info = response.json()
            
            print(f"✅ Torrent details:")
            print(f"   ID: {torrent_info.get('id')}")
            print(f"   Name: {torrent_info.get('filename', 'Unknown')}")
            print(f"   Status: {torrent_info.get('status', 'Unknown')}")
            print(f"   Hash: {torrent_info.get('hash', 'Unknown')}")
            print(f"   Size: {torrent_info.get('bytes', 0):,} bytes")
            print(f"   Progress: {torrent_info.get('progress', 0)}%")
            print(f"   Original filename: {torrent_info.get('original_filename', 'N/A')}")
            print(f"   Added: {torrent_info.get('added', 'Unknown')}")
            
            files = torrent_info.get('files', [])
            print(f"   Files count: {len(files)}")
            
            if files:
                print(f"\n📁 File details:")
                video_extensions = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.mpg', '.mpeg', '.3gp', '.ogv')
                
                video_files = []
                other_files = []
                
                for file in files:
                    filename = file.get('path', '').split('/')[-1]
                    if filename.lower().endswith(video_extensions):
                        video_files.append(file)
                    else:
                        other_files.append(file)
                
                print(f"   Video files: {len(video_files)}")
                for i, file in enumerate(video_files[:5]):  # Show first 5 video files
                    selected = "✓" if file.get('selected') == 1 else "✗"
                    size_mb = file.get('bytes', 0) / (1024 * 1024)
                    print(f"     {selected} {file.get('path', 'Unknown')} ({size_mb:.1f} MB)")
                
                if len(video_files) > 5:
                    print(f"     ... and {len(video_files) - 5} more video files")
                
                print(f"   Other files: {len(other_files)}")
                if other_files:
                    for file in other_files[:3]:  # Show first 3 other files
                        selected = "✓" if file.get('selected') == 1 else "✗"
                        size_mb = file.get('bytes', 0) / (1024 * 1024)
                        print(f"     {selected} {file.get('path', 'Unknown')} ({size_mb:.1f} MB)")
                    if len(other_files) > 3:
                        print(f"     ... and {len(other_files) - 3} more other files")
            
            return torrent_info
        except Exception as e:
            print(f"❌ Failed to get torrent details: {e}")
            return None
    
    def test_add_torrent_simulation(self, infohash: str) -> Optional[str]:
        """Simulate adding a torrent (for testing purposes)"""
        print(f"\n🧪 Testing torrent addition for: {infohash}")
        
        magnet = f"magnet:?xt=urn:btih:{infohash}"
        print(f"   Magnet URI: {magnet}")
        
        # For safety, we'll just show what would happen without actually adding
        print("   [SIMULATION MODE - Not actually adding torrent]")
        print("   Would POST to: /torrents/addMagnet")
        print(f"   With data: {{'magnet': '{magnet.lower()}'}}")
        
        return None  # Don't actually add
    
    def compare_with_riven_logic(self, target_hash: str, torrents: List[Dict[str, Any]]):
        """Replicate Riven's _find_existing_torrent logic exactly"""
        print(f"\n🔄 Replicating Riven's _find_existing_torrent logic...")
        
        # This is exactly what Riven does:
        search_hash = target_hash.lower().strip()
        print(f"   Normalized hash: {search_hash}")
        
        found_torrent = None
        for torrent in torrents:
            torrent_hash = torrent.get("hash", "").lower().strip()
            if torrent_hash == search_hash:
                found_torrent = torrent
                break
        
        if found_torrent:
            print(f"✅ Riven logic would find:")
            print(f"   Torrent ID: {found_torrent['id']}")
            print(f"   Status: {found_torrent.get('status', 'unknown')}")
            print(f"   Name: {found_torrent.get('filename', 'Unknown')}")
        else:
            print(f"❌ Riven logic would NOT find the torrent")
            print(f"   This explains why it tries to add a new one")
    
    def full_debug_for_hash(self, target_hash: str):
        """Run complete debug workflow for a specific hash"""
        print("🚀 Starting Real-Debrid Debug Session")
        print("=" * 60)
        print(f"Target hash: {target_hash}")
        print("=" * 60)
        
        # Step 1: Test API connection
        if not self.test_api_connection():
            return
        
        # Step 2: Get all torrents
        torrents = self.get_all_torrents()
        if not torrents:
            return
        
        # Step 3: Search for the specific hash
        found_torrent = self.search_for_hash(target_hash, torrents)
        
        # Step 4: If found, analyze details
        if found_torrent:
            self.analyze_torrent_details(found_torrent['id'], target_hash)
        
        # Step 5: Compare with Riven's logic
        self.compare_with_riven_logic(target_hash, torrents)
        
        # Step 6: Show recommendations
        print(f"\n💡 Recommendations:")
        if found_torrent:
            status = found_torrent.get('status', 'unknown')
            if status == 'downloaded':
                print("   ✅ Torrent is downloaded and should be accessible")
                print("   ➤ Check if Riven's file processing logic is working correctly")
            elif status in ['downloading', 'queued']:
                print("   ⏳ Torrent is still downloading")
                print("   ➤ Wait for download to complete")
            elif status == 'waiting_files_selection':
                print("   ⚠️ Torrent needs file selection")
                print("   ➤ Select video files in Real-Debrid web interface")
            else:
                print(f"   ❓ Torrent has unusual status: {status}")
                print("   ➤ Check Real-Debrid web interface for more details")
        else:
            print("   ❌ Torrent not found in API response")
            print("   ➤ Check if the hash is correct")
            print("   ➤ Verify you're using the same Real-Debrid account")
            print("   ➤ Check if there are API rate limits or temporary issues")
        
        print("\n✅ Debug session completed!")

def get_api_key_from_env_or_input() -> Optional[str]:
    """Get API key from environment or user input"""
    # Try to get from environment first
    api_key = os.getenv('REAL_DEBRID_API_KEY')
    
    if not api_key:
        # Try to read from Riven config if available
        try:
            # This is a simplified way - you might need to adjust based on your actual config
            print("🔑 API key not found in environment.")
            print("Please enter your Real-Debrid API key:")
            api_key = input().strip()
            
            if not api_key:
                print("❌ No API key provided")
                return None
        except KeyboardInterrupt:
            print("\n❌ Cancelled by user")
            return None
    
    return api_key

def main():
    """Main function"""
    print("🔧 Real-Debrid Debug Tool for Riven")
    print("=" * 40)
    
    # The problematic hash from your logs
    TARGET_HASH = "A754E2E9DC1F61F259F0FEA688BCEE1858DFA013"
    
    # Get API key
    api_key = get_api_key_from_env_or_input()
    if not api_key:
        return
    
    # Run debug session
    debugger = RealDebridDebugger(api_key)
    debugger.full_debug_for_hash(TARGET_HASH)

if __name__ == "__main__":
    main()
