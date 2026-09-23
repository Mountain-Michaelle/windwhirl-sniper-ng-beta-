from apps.oms.events.dispatcher import EventDispatcher

# Global singleton event dispatcher
# Import this in any module that emits or listens to events:
#   from app.oms.events import dispatcher
#   dispatcher.on("order.detected")(my_handler)
#   await dispatcher.emit("order.detected", order=order)
dispatcher = EventDispatcher()

__all__ = ["EventDispatcher", "dispatcher"]