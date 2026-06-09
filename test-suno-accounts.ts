import { execSync } from 'child_process';

// Simple ADB automation for testing Suno account creation
class ADBAutomation {
  private deviceIds: string[] = [];

  constructor() {
    this.detectVMOSDevices();
  }

  // Find all running VMOS devices
  private detectVMOSDevices(): void {
    try {
      const output = execSync('adb devices').toString();
      const lines = output.split('\n');
      
      // Skip header ("List of devices attached") and empty lines
      for (let i = 1; i < lines.length; i++) {
        const line = lines[i].trim();
        if (line && !line.startsWith('List of')) {
          const deviceId = line.split('\t')[0];
          this.deviceIds.push(deviceId);
          console.log(`✅ Found VMOS device: ${deviceId}`);
        }
      }

      if (this.deviceIds.length === 0) {
        console.log('❌ No VMOS devices found. Make sure adb devices shows your devices.');
        process.exit(1);
      }

      console.log(`📱 Found ${this.deviceIds.length} VMOS devices: ${this.deviceIds.join(', ')}`);
    } catch (error) {
      console.error('❌ Failed to detect devices:', error);
      process.exit(1);
    }
  }

  // Install APK if needed
  private ensureAPKInstalled(deviceId: string): void {
    try {
      // Check if Suno app is installed
      const apkName = 'com.suno.app';
      execSync(`adb -s ${deviceId} shell pm list packages | grep ${apkName}`);
      console.log(`📲 Suno app already installed on ${deviceId}`);
    } catch (error) {
      console.warn(`⚠️  Suno app not found on ${deviceId}. Please install it manually.`);
    }
  }

  // Launch Suno app
  private launchSunoApp(deviceId: string): void {
    try {
      // Launch Suno app (adjust package name if different)
      execSync(`adb -s ${deviceId} shell am start -n com.suno.app/com.suno.app.MainActivity`);
      console.log(`🚀 Launching Suno app on ${deviceId}`);
      
      // Wait for app to load
      this.wait(3000);
    } catch (error) {
      console.error(`❌ Failed to launch Suno on ${deviceId}:`, error);
      throw error;
    }
  }

  // Type text using ADB (handles special characters)
  private typeText(deviceId: string, text: string): void {
    try {
      // ADB input text command
      execSync(`adb -s ${deviceId} shell input text "${this.escapeADBText(text)}"`);
      this.wait(500); // Small delay after typing
    } catch (error) {
      console.error(`❌ Failed to type text on ${deviceId}:`, error);
      throw error;
    }
  }

  // Tap at coordinates (X Y)
  private tap(deviceId: string, x: number, y: number): void {
    try {
      execSync(`adb -s ${deviceId} shell input tap ${x} ${y}`);
      this.wait(800); // Wait after tap
    } catch (error) {
      console.error(`❌ Failed to tap on ${deviceId}:`, error);
      throw error;
    }
  }

  // Escape special characters for ADB input text
  private escapeADBText(text: string): string {
    return text
      .replace(/"/g, '\\"')  // Escape quotes
      .replace(/\\/g, '\\\\') // Escape backslashes
      .replace(/\n/g, '\\n')  // Escape newlines
      .replace(/\t/g, '\\t'); // Escape tabs
  }

  // Wait in milliseconds
  private wait(ms: number): void {
    const start = Date.now();
    while (Date.now() - start < ms) {
      // Busy wait (good for automation scripts)
    }
  }

  // Test Instagram login on a device
  private testInstagramLogin(deviceId: string, email: string, password: string): void {
    console.log(`🔗 Testing Instagram login on ${deviceId}`);
    
    try {
      // Launch Instagram app
      execSync(`adb -s ${deviceId} shell am start -n com.instagram.android/com.instagram.android.MainTabActivity`);
      this.wait(4000);
      
      // Tap login button (adjust coordinates for your screen size)
      this.tap(deviceId, 360, 1000); // Approximate login button position
      
      this.wait(2000);
      
      // Type email
      this.typeText(deviceId, email);
      this.wait(1000);
      
      // Type password  
      this.tap(deviceId, 360, 1200); // Move to password field
      this.wait(500);
      this.typeText(deviceId, password);
      this.wait(1000);
      
      // Tap login
      this.tap(deviceId, 360, 1400); // Login button position
      
      console.log(`✅ Instagram login attempt started on ${deviceId}`);
      
    } catch (error) {
      console.error(`❌ Instagram login failed on ${deviceId}:`, error);
    }
  }

  // Create a Suno account on a device (simplified test)
  private createSunoAccount(deviceId: string, email: string, password: string): void {
    console.log(`🌟 Creating Suno account on ${deviceId}: ${email}`);
    
    try {
      this.ensureAPKInstalled(deviceId);
      this.launchSunoApp(deviceId);
      
      // Wait for app to load and show sign up screen
      this.wait(5000);
      
      // Tap sign up button (approximate coordinates for your screen)
      this.tap(deviceId, 300, 800); 
      this.wait(2000);
      
      // Type email
      this.typeText(deviceId, email);
      this.wait(1000);
      
      // Type password
      this.tap(deviceId, 300, 900); // Move to password field
      this.wait(500);
      this.typeText(deviceId, password);
      this.wait(1000);
      
      // Tap create account
      this.tap(deviceId, 300, 1000); // Create account button
      this.wait(3000);
      
      console.log(`✅ Suno account creation started on ${deviceId}`);
      
    } catch (error) {
      console.error(`❌ Suno account creation failed on ${deviceId}:`, error);
    }
  }

  // Run parallel tests on all devices
  public runParallelTests(): void {
    console.log('\n🚀 Starting parallel Suno account creation tests...\n');

    // Test credentials (can be real or test accounts)
    const testCredentials = [
      { email: 'test1@suno.com', password: 'TestPass123!' },
      { email: 'test2@suno.com', password: 'TestPass456!' },
      { email: 'test3@suno.com', password: 'TestPass789!' },
    ];

    // Run on each device
    this.deviceIds.forEach((deviceId, index) => {
      const credential = testCredentials[index % testCredentials.length];
      
      console.log(`\n📱 Processing device ${deviceId} with ${credential.email}`);
      
      // Run Suno account creation in parallel
      this.createSunoAccount(deviceId, credential.email, credential.password);
      
      // Also test Instagram login
      this.testInstagramLogin(deviceId, credential.email, credential.password);
    });

    console.log('\n🎉 All parallel tasks initiated!');
    console.log('📲 Watch each device screen to see the automation in action.');
    console.log('⏱️  Each device should be typing and tapping automatically.');
  }
}

// Main execution
try {
  const automation = new ADBAutomation();
  automation.runParallelTests();
} catch (error) {
  console.error('❌ Automation failed:', error);
  process.exit(1);
}