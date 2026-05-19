import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import './index.css'
import Login from './pages/Login.tsx'
import { AppProviders } from './app/providers.tsx'
import { AppShell } from './app/AppShell.tsx'
import { Routes, Route } from 'react-router-dom'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders>
      <BrowserRouter>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/*" element={<AppShell />} />
        </Routes>
      </BrowserRouter>
    </AppProviders>
  </StrictMode>,
)
