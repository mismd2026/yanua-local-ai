// 受付端末の入口（/kiosk）。社内の画面とは別に組み立て、社内の画面のコードを含めない
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import Kiosk from "./Kiosk";
import "./kiosk.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <Kiosk />
  </StrictMode>,
);
