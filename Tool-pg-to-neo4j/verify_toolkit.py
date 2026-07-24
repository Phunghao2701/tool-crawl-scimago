#!/usr/bin/env python3
"""
Installation Verification Script
Verifies that all setup files are present and working
"""

import os
import subprocess
import sys
from pathlib import Path
from datetime import datetime

class Verifier:
    def __init__(self):
        self.project_root = Path(__file__).parent
        self.passed = []
        self.failed = []
        self.warnings = []
        
    def log_pass(self, msg):
        self.passed.append(msg)
        print(f"  ✅ {msg}")
        
    def log_fail(self, msg):
        self.failed.append(msg)
        print(f"  ❌ {msg}")
        
    def log_warn(self, msg):
        self.warnings.append(msg)
        print(f"  ⚠️  {msg}")
    
    def check_toolkit_files(self):
        """Check if all toolkit files exist"""
        print("\n📋 Checking Toolkit Files")
        print("-" * 50)
        
        required_files = {
            "setup_tool.py": "Main setup automation script",
            "quick_start.py": "Interactive setup guide",
            "setup.bat": "Windows setup script",
            "START_HERE.md": "Quick start guide",
            "TOOLKIT_README.md": "Complete documentation",
            "AI_IMPLEMENTATION_GUIDE_GRAPH_SYNC_TOOL.md": "Original guide",
        }
        
        for filename, description in required_files.items():
            filepath = self.project_root / filename
            if filepath.exists():
                size = filepath.stat().st_size
                self.log_pass(f"{filename} ({size:,} bytes)")
            else:
                self.log_fail(f"{filename} - {description}")
    
    def check_python(self):
        """Check Python version and packages"""
        print("\n🐍 Checking Python Environment")
        print("-" * 50)
        
        # Check version
        version_info = sys.version_info
        if version_info.major >= 3 and version_info.minor >= 9:
            self.log_pass(f"Python {version_info.major}.{version_info.minor}.{version_info.micro}")
        else:
            self.log_fail(f"Python 3.9+ required (found {version_info.major}.{version_info.minor})")
    
    def check_docker(self):
        """Check Docker installation"""
        print("\n🐳 Checking Docker Installation")
        print("-" * 50)
        
        # Check Docker
        try:
            result = subprocess.run(
                ["docker", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                self.log_pass(version)
            else:
                self.log_fail("Docker not responding")
        except FileNotFoundError:
            self.log_fail("Docker not found in PATH")
            self.log_warn("Install Docker Desktop from https://www.docker.com/")
        except Exception as e:
            self.log_fail(f"Docker check failed: {e}")
        
        # Check Docker Compose
        try:
            result = subprocess.run(
                ["docker-compose", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                self.log_pass(version)
            else:
                self.log_fail("Docker Compose not responding")
        except FileNotFoundError:
            self.log_fail("Docker Compose not found in PATH")
        except Exception as e:
            self.log_fail(f"Docker Compose check failed: {e}")
    
    def check_script_permissions(self):
        """Check if scripts have correct permissions"""
        print("\n🔐 Checking Script Permissions")
        print("-" * 50)
        
        py_files = [
            "setup_tool.py",
            "quick_start.py",
        ]
        
        for filename in py_files:
            filepath = self.project_root / filename
            if filepath.exists():
                # Check if readable
                if os.access(filepath, os.R_OK):
                    self.log_pass(f"{filename} is readable")
                else:
                    self.log_fail(f"{filename} is not readable")
                
                # Check if executable
                if os.access(filepath, os.X_OK):
                    self.log_pass(f"{filename} is executable")
                else:
                    self.log_warn(f"{filename} is not executable (can be run with python)")
    
    def check_documentation(self):
        """Check documentation files"""
        print("\n📚 Checking Documentation")
        print("-" * 50)
        
        docs = {
            "START_HERE.md": "Quick start guide (read this first)",
            "TOOLKIT_README.md": "Complete toolkit documentation",
            "AI_IMPLEMENTATION_GUIDE_GRAPH_SYNC_TOOL.md": "Original project guide",
        }
        
        for filename, description in docs.items():
            filepath = self.project_root / filename
            if filepath.exists():
                size = filepath.stat().st_size
                # Count lines
                lines = filepath.read_text().count('\n')
                self.log_pass(f"{filename} ({lines} lines)")
            else:
                self.log_fail(f"{filename}")
    
    def generate_report(self):
        """Generate verification report"""
        print("\n" + "="*60)
        print("VERIFICATION REPORT")
        print("="*60)
        
        total_tests = len(self.passed) + len(self.failed) + len(self.warnings)
        passed_count = len(self.passed)
        
        print(f"\n📊 Results:")
        print(f"  ✅ Passed: {passed_count}")
        print(f"  ❌ Failed: {len(self.failed)}")
        print(f"  ⚠️  Warnings: {len(self.warnings)}")
        print(f"  📈 Total: {total_tests}")
        
        if not self.failed:
            print(f"\n✅ All checks passed! ({passed_count}/{total_tests})")
            return True
        else:
            print(f"\n⚠️  Some checks failed. Review above for details.")
            return False
    
    def show_next_steps(self):
        """Show next steps"""
        print("\n" + "="*60)
        print("NEXT STEPS")
        print("="*60)
        print("""
1. READ: START_HERE.md
   ├─ Quickest way to get started
   ├─ 3 simple steps
   └─ Takes 5 minutes to understand

2. RUN: The Setup Tool
   ├─ Windows: setup.bat
   ├─ macOS/Linux: python3 quick_start.py
   └─ Or: python setup_tool.py

3. WAIT: For setup to complete (2-3 minutes)
   ├─ Automatic directory creation
   ├─ Configuration generation
   ├─ Docker setup creation
   └─ Documentation generation

4. START: Services with Docker
   ├─ cd research-graph-sync
   ├─ docker-compose -f docker/docker-compose.yml up -d
   └─ Wait 30-60 seconds

5. VERIFY: Everything is working
   ├─ curl http://localhost:8000/health
   ├─ Open http://localhost:7474 (Neo4j)
   └─ Check: docker-compose -f docker/docker-compose.yml ps

6. LOAD: Your data
   ├─ Import into PostgreSQL
   ├─ Run full sync: POST /sync/full
   └─ Monitor in Neo4j Browser
""")
    
    def run_all_checks(self):
        """Run all verification checks"""
        print("\n" + "="*60)
        print("🔍 VERIFICATION SCRIPT FOR RESEARCH GRAPH SYNC TOOLKIT")
        print("="*60)
        
        self.check_toolkit_files()
        self.check_python()
        self.check_docker()
        self.check_script_permissions()
        self.check_documentation()
        
        success = self.generate_report()
        self.show_next_steps()
        
        return success


def main():
    """Main entry point"""
    verifier = Verifier()
    success = verifier.run_all_checks()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
