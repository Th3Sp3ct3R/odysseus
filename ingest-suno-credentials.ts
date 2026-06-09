/**
 * ingest-suno-credentials.ts
 * 
 * Ingests raw email:password pairs into the Suno Account Creation Queue,
 * automatically assigning them to available, unassigned VMOS devices.
 * 
 * Usage: npx tsx ingest-suno-credentials.ts
 */

import { neon } from '@neondatabase/serverless';
import dotenv from 'dotenv';

dotenv.config();

const sql = neon(process.env.DATABASE_URL!);

const RAW_CREDENTIALS = `kksljlcv@polosmail.com:gzprtlfoY!4929
cqqwqxwr@tacoblastmail.com:kagakiodY!6147
shjjyhan@vargosmail.com:qpixktpsS!7100
unhaunvi@fringmail.com:cywhtdwyS!4631
zfzmzlpm@polosmail.com:qhswwchoA!3049
qdfsjiwk@tacoblastmail.com:fpskbdiwS!1821
dmikiaun@vargosmail.com:zeheyxpkY!4454
frfxlhak@fringmail.com:dfselnesS!6544
qflvekql@polosmail.com:prqhivjoY!2437
chezjlpx@tacoblastmail.com:qrfdudgnX!3094
dtxpowur@vargosmail.com:dzicehaqY!8084
iketrevo@fringmail.com:rpkuzumhY!6736
vdrhybwh@polosmail.com:xspjvmxmX!1972
fluesnjv@tacoblastmail.com:pkbpfzetY!3397
fnofnyfw@vargosmail.com:jdndsscoS!9881
zijbahia@fringmail.com:frzdkpndY!8713
zctcbmvd@polosmail.com:pyfkhsfxX!1752
fpfjyyyn@tacoblastmail.com:qguirwpmS!9747
uhbfurxn@vargosmail.com:zlkyxflqS!1605
ififxnuk@fringmail.com:tueogvgqS!8321
qyahcojd@polosmail.com:ltbjhuusX!2502
klrbleuo@tacoblastmail.com:erwyuzrxX!4610
wxwreatp@vargosmail.com:texpmihbA!1544
kpaopzbf@fringmail.com:tpicqdlvY!1871
fpnenjsh@polosmail.com:udhmwyyaY!3029`;

async function main() {
  console.log('🔍 Fetching available VMOS devices...');
  
  // Find devices that are 'running' and not currently assigned to an active Suno account
  const availableDevices = await sql`
    SELECT v.pad_code, v.adb_host, v.adb_port 
    FROM vmos_devices v
    LEFT JOIN suno_accounts sa ON v.pad_code = sa.device_id AND sa.status = 'active'
    WHERE v.status = 'running' 
      AND sa.device_id IS NULL
    LIMIT 30
  `;

  if (availableDevices.length === 0) {
    console.error('❌ No available/running VMOS devices found. Start some pads first.');
    return;
  }

  console.log(\`✅ Found \${availableDevices.length} available devices.\`);

  const lines = RAW_CREDENTIALS.trim().split('\\n');
  console.log(\`📦 Processing \${lines.length} credential pairs...\`);

  let successCount = 0;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line.includes(':')) continue;

    const [email, password] = line.split(':');
    const assignedDevice = availableDevices[i % availableDevices.length];

    try {
      await sql\`
        INSERT INTO suno_account_creation_queue (
          email, 
          password, 
          assigned_device_id, 
          status, 
          created_at
        ) VALUES (
          \${email}, 
          \${password}, 
          \${assignedDevice.pad_code}, 
          'pending', 
          NOW()
        )
        ON CONFLICT (email) DO NOTHING;
      \`;
      successCount++;
      console.log(\`  ✅ Queued: \${email} -> Device: \${assignedDevice.pad_code}\`);
    } catch (err) {
      console.error(\`  ❌ Failed to queue \${email}:\`, err);
    }
  }

  console.log(\`\\n🎉 Successfully queued \${successCount}/\${lines.length} accounts for creation.\`);
  console.log('💡 Next step: Start the SunoAccountCreator worker: npx tsx src/workers/SunoAccountCreator.ts');
}

main().catch(console.error);
