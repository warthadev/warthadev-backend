import express from "express";
import fetch from "node-fetch";
import cors from "cors";

const app = express();
app.use(express.json());
app.use(cors({ origin: "*" })); // sementara semua origin, nanti bisa dibatasi

app.post("/trigger", async (req, res) => {
  const { url } = req.body;
  if (!url) return res.status(400).json({ error: "URL gak boleh kosong" });

  try {
    // sementara log dulu, nanti bisa diarahkan ke Colab webhook
    console.log("✅ Trigger diterima:", url);
    res.json({ message: `URL diterima: ${url}` });
  } catch (err) {
    console.error(err);
    res.status(500).json({ error: "Gagal trigger" });
  }
});

const port = process.env.PORT || 8080;
app.listen(port, () => console.log(`🚀 Server jalan di port ${port}`));