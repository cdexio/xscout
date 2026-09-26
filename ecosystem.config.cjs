// pm2 process definition. Start: pm2 start ecosystem.config.cjs ; logs: pm2 logs xscout
module.exports = {
  apps: [
    {
      name: "xscout",
      cwd: __dirname,
      script: "uv",
      args: "run xscout serve",
      interpreter: "none",
      autorestart: true,
      max_restarts: 20,
      restart_delay: 5000,
      kill_timeout: 15000,
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
