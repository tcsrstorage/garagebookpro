// Full Firebase Realtime Database backup script for GarageBookPro.
// Uses the Firebase Admin SDK with a service account, which bypasses
// security rules entirely — this gives a TRUE full backup (including
// admin-only nodes like redeemCodes, adminUserDirectory, supportMessages,
// activityLog, referralCodes, etc. — not just what a regular user could read).

const admin = require('firebase-admin');
const fs = require('fs');
const path = require('path');

const serviceAccount = JSON.parse(process.env.FIREBASE_SERVICE_ACCOUNT_JSON);

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
  databaseURL: 'https://garagebookpro-default-rtdb.asia-southeast1.firebasedatabase.app'
});

async function runBackup() {
  const db = admin.database();
  const snapshot = await db.ref('/').once('value');
  const data = snapshot.val() || {};

  const backupsDir = path.join(__dirname, '..', 'backups');
  if (!fs.existsSync(backupsDir)) fs.mkdirSync(backupsDir, { recursive: true });

  const now = new Date();
  const stamp = now.toISOString().replace(/[:.]/g, '-').slice(0, 19); // e.g. 2026-07-23T02-00-00
  const filename = `backup-${stamp}.json`;
  const filepath = path.join(backupsDir, filename);

  fs.writeFileSync(filepath, JSON.stringify(data, null, 2));
  console.log(`Backup written: ${filepath}`);

  // Retain only the last 12 backups.
  const files = fs.readdirSync(backupsDir)
    .filter(f => f.startsWith('backup-') && f.endsWith('.json'))
    .sort(); // ISO timestamp sorts chronologically as string
  const excess = files.length - 12;
  if (excess > 0) {
    for (let i = 0; i < excess; i++) {
      fs.unlinkSync(path.join(backupsDir, files[i]));
      console.log(`Removed old backup: ${files[i]}`);
    }
  }

  process.exit(0);
}

runBackup().catch(err => {
  console.error('Backup failed:', err);
  process.exit(1);
});
