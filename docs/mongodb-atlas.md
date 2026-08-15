# MongoDB Atlas setup

This project stores an encrypted authenticated HSC session in MongoDB, so you need a
MongoDB database to use `refresh-session`, `monitor-once` and `init-config`.

[MongoDB Atlas](https://www.mongodb.com/cloud/atlas) offers a free tier that is
sufficient for this application: it stores only three small state documents
(session, monitor state, availability snapshot).

## Prerequisites

* A [MongoDB Atlas account](https://account.mongodb.com/account/register) (free)
* The ability to open outbound HTTPS connections to MongoDB's servers

## Setup steps

### 1. Create a project

Log into [MongoDB Atlas](https://cloud.mongodb.com), and create a new project.

### 2. Create a free cluster

Under **Clusters**, click **Create** and select **M0 (free)** tier. Choose your
preferred cloud provider and region (they do not affect this application's
functionality). Complete the cluster creation.

### 3. Create a database user

Under your cluster, go to **Security → Database Access** and add a new database user:

* **Username:** something memorable, e.g., `hsc-monitor`
* **Password:** a strong password (save it, you will need it)
* **Database User Privileges:** Read and write to any database

This is *not* your Atlas account user — it is an application-specific database
user that MongoDB will require for every connection.

### 4. Configure network access

Under **Security → Network Access**, add a new IP access list entry:

* To access from **any IP** (less secure, simpler for personal use):
  ```
  0.0.0.0/0
  ```

* To restrict to **specific IPs** (more secure):
  Add the GitHub Actions runner IP ranges if you know them, or your office/home
  IP. Note: GitHub-hosted runners use dynamic IPs, so a single fixed IP is not
  guaranteed. Self-hosted runners are more flexible.

### 5. Get the connection string

Under your cluster, click **Connect**, then **Drivers**. Copy the Python 3.12+
connection string example. It will look like:

```
mongodb+srv://hsc-monitor:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority
```

Replace `<password>` with the database user's password you created in step 3.

This is your `HSC_MONGODB_URI` secret.

### 6. Generate the encryption key

The stored session is encrypted with Fernet (authenticated encryption). Generate
a key locally:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

This output is your `HSC_SESSION_ENCRYPTION_KEY` secret. **Keep it secret, and
store the same key everywhere it is needed** (locally in `.env` and in the
GitHub Environment).

Losing this key makes previously encrypted sessions unreadable. If you rotate
the key, you must refresh the session with the new key.

## Local setup

Add both to your `.env`:

```bash
HSC_MONGODB_URI=mongodb+srv://hsc-monitor:<password>@cluster0.mongodb.net/?retryWrites=true&w=majority
HSC_SESSION_ENCRYPTION_KEY=<your-generated-key>
```

Test the connection by running `refresh-session`:

```bash
python -m hsc_queue_monitor.cli refresh-session
```

If it succeeds, the session document has been created in MongoDB. You can verify
it in the Atlas console under **Collections → hsc → hsc-api-session** (you may
need to refresh the browser).

## GitHub Actions setup

In your repository, go to **Settings → Environments → production** and add two
environment secrets:

| Secret | Value |
|--------|-------|
| `HSC_MONGODB_URI` | The connection string from step 5 |
| `HSC_SESSION_ENCRYPTION_KEY` | The generated key from step 6 |

Use the *exact same* encryption key on both local and GitHub — they must decrypt
the same session documents.

---

## Network considerations

GitHub-hosted runners request connections from dynamic IP ranges, which can change
over time. If your MongoDB Atlas network access is restricted to specific IPs:

* **Safest:** Use a self-hosted runner with a static IP, or a VPN/bastion that
  provides one.
* **Current:** Allow the GitHub Actions IP ranges. MongoDB publishes them, but
  they change regularly.
* **Simple/risky:** Allow `0.0.0.0/0` (any IP). This is acceptable for a
  personal project with strong database credentials and encryption keys, but
  increases exposure. Use a strong database password.

---

## Database initialization

The application creates and updates its own documents automatically:

| Collection | Documents |
|------------|-----------|
| `hsc-api-session` | The encrypted authenticated HSC session |
| `hsc-monitor-state` | Monitor state: `READY`, `AUTH_REQUIRED`, `RATE_LIMITED`, `SERVICE_UNAVAILABLE` |
| `hsc-availability-snapshot` | The last complete availability scan (for change detection) |

You do not need to manually create collections or documents, provided your
database user has read/write permissions to the configured database.

---

## Troubleshooting

**Connection refused:** Check that your IP is in the Network Access list and
that `HSC_MONGODB_URI` is correct (including the password).

**Authentication failed:** Verify the database user name and password match.
Remember: the database user is *not* your Atlas account user.

**"No servers available":** The cluster may still be starting. Wait a few minutes
and try again.

**Permission denied on collections:** Ensure the database user has read/write
permissions, not just read.
